"""Turn filing text into vectors and search them in Amazon S3 Vectors.

The path for one filing:

    plain text  ->  chunks of words  ->  OpenAI embedding (1,024 numbers)
                ->  PutVectors into the index  ->  later, QueryVectors

An embedding is a list of 1,024 numbers that stands for the meaning of a
passage. Two passages about the same idea land near each other even when
they use different words. The index you created (`filing-chunks`) requires
every list to be exactly 1,024 numbers long and compares them with cosine
distance. A smaller distance means a closer meaning.

The chunk's words are stored in metadata under the key `text`. That key has
to be the non-filterable metadata key on the index. Filterable metadata is
capped at 2 KB, and a passage is larger than that. `ticker` and `accession`
stay filterable so a query can look inside one filing only.

Chunk 0 is written last on purpose. `GetVectors` for key `{accession}#0000`
is the "this filing is fully stored" check. If the process dies halfway,
chunk 0 is missing and the next search writes the filing again. PutVectors
replaces a key that already exists, so a retry does not create duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from openai import OpenAI, OpenAIError

from utils.sections import FilingSection, extract_sections

# Same .env the rest of the project uses. Importing this module from the MCP
# subprocess still sees VECTOR_BUCKET, the AWS keys, and the embedding model.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# Must match the index created in the S3 console. Changing either number in
# only one place makes PutVectors fail, because every vector in an index has
# one fixed length and one distance metric.
EMBEDDING_DIMENSIONS = 1024

# About a page of filing text. Short enough for the embedding model's input
# limit and for the 40 KB metadata cap, long enough that a 10-K is hundreds
# of chunks rather than thousands.
_WORDS_PER_CHUNK = 450
# Repeated words at the edge of each chunk so a sentence that crosses the
# boundary still appears whole in one of the two chunks.
_OVERLAP_WORDS = 50
# OpenAI accepts many passages in one embeddings call. A modest batch stays
# under the request token limit while a whole 10-K is still only a few calls.
_EMBED_BATCH_SIZE = 16
# PutVectors accepts up to 500 vectors per call. Smaller batches keep the
# request comfortably under the 20 MB payload limit.
_PUT_BATCH_SIZE = 100
# Stored in every chunk's metadata and in its key. A query filters on this
# value, so older chunks are not returned and the next search rebuilds the
# filing. v3 adds the 10-Q part to the section label ("Part II, Item 1A").
CHUNK_VERSION = "v3"


class VectorSearchError(Exception):
    """A setup, OpenAI, or AWS problem the tool should show to the model as text."""


def _settings() -> tuple[str, str, str, str]:
    """Read the bucket, index, embedding model, and region from the environment."""
    bucket = os.getenv("VECTOR_BUCKET", "").strip()
    index = os.getenv("VECTOR_INDEX", "").strip()
    model_id = os.getenv("EMBEDDING_MODEL", "").strip()
    region = os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION") or ""
    missing = [
        name
        for name, value in (
            ("VECTOR_BUCKET", bucket),
            ("VECTOR_INDEX", index),
            ("EMBEDDING_MODEL", model_id),
            ("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", "").strip()),
            ("AWS_DEFAULT_REGION", region),
        )
        if not value
    ]
    if missing:
        raise VectorSearchError(
            "Set " + ", ".join(missing) + " in .env before searching filings."
        )
    return bucket, index, model_id, region


def _clients() -> tuple[OpenAI, object]:
    """An OpenAI client (makes the vectors) and an S3 Vectors client (stores them).

    OpenAI() reads OPENAI_API_KEY from the environment. boto3 reads
    AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY the same way. The numbers
    are created outside AWS, then written into the index you already made.
    """
    _bucket, _index, _model_id, region = _settings()
    return (
        OpenAI(),
        boto3.client("s3vectors", region_name=region),
    )


def _aws_message(exc: Exception) -> str:
    """Pull the short code and sentence out of a boto3 error."""
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return f"{err.get('Code', 'ClientError')}: {err.get('Message', '')}"
    return str(exc)


def chunk_text(text: str) -> list[str]:
    """Split filing text into overlapping passages.

    `split()` collapses the blank lines and table spacing left over from the
    HTML conversion, so a chunk is a run of words rather than a run of lines.
    """
    words = text.split()
    if not words:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + _WORDS_PER_CHUNK, len(words))
        piece = " ".join(words[start:end]).strip()
        if piece:
            chunks.append(piece)
        if end >= len(words):
            break
        # Step forward by less than a full chunk so the next one repeats the tail.
        start = end - _OVERLAP_WORDS
    return chunks


def _vector_key(accession: str, chunk_index: int) -> str:
    """Stable id for one chunk. The version keeps a new chunker from reusing old keys."""
    return f"{accession}#{CHUNK_VERSION}#{chunk_index:04d}"


def _section_chunks(form: str, text: str) -> list[dict[str, str]]:
    """Keep the configured sections, then cut each one into embedding-sized pieces.

    The label is the first line of the stored text. The model copies that text
    into sources, and the UI shows it, so the excerpt names its section.
    """
    sections = extract_sections(text, form)
    if not sections:
        # No Item headings matched. Search the whole filing rather than return nothing.
        sections = [FilingSection(form or "filing", "", "Full filing", None, text)]

    rows: list[dict[str, str]] = []
    for section in sections:
        for piece in chunk_text(section.text):
            rows.append({
                "text": f"[{section.label}]\n{piece}",
                "form": form,
                "item": section.item,
                "section": section.label,
            })
    return rows


def _embed_all(openai_client: OpenAI, model_id: str, texts: list[str]) -> list[list[float]]:
    """Ask OpenAI for the 1,024 numbers that represent each text.

    `text-embedding-3-small` can shorten its normal output to 1,024 numbers.
    That length is required: the S3 index was created with 1,024 dimensions
    and will reject a list of any other length. OpenAI already returns
    vectors with length 1, which is what cosine comparison expects.

    One request can embed many chunks. The response includes an `index` on
    each item because the items are not guaranteed to come back in order.
    """
    embeddings: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH_SIZE):
        batch = texts[start:start + _EMBED_BATCH_SIZE]
        response = openai_client.embeddings.create(
            model=model_id,
            input=batch,
            dimensions=EMBEDDING_DIMENSIONS,
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        for item in ordered:
            if len(item.embedding) != EMBEDDING_DIMENSIONS:
                raise VectorSearchError(
                    f"{model_id} returned {len(item.embedding)} numbers; "
                    f"the index expects {EMBEDDING_DIMENSIONS}."
                )
            embeddings.append(list(item.embedding))
    return embeddings


def _already_stored(s3vectors, bucket: str, index: str, accession: str) -> bool:
    """True when chunk 0 is already in the index, which means the write finished."""
    response = s3vectors.get_vectors(
        vectorBucketName=bucket,
        indexName=index,
        keys=[_vector_key(accession, 0)],
        returnMetadata=False,
        returnData=False,
    )
    stored = response.get("vectors") or []
    return any(item.get("key") == _vector_key(accession, 0) for item in stored)


def _put_chunks(
    s3vectors,
    bucket: str,
    index: str,
    ticker: str,
    accession: str,
    chunks: list[dict[str, str]],
    embeddings: list[list[float]],
) -> None:
    """Write every chunk. Chunk 0 goes in the last batch so a crash cannot look finished."""
    records = []
    for chunk_index, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        records.append({
            "key": _vector_key(accession, chunk_index),
            "data": {"float32": embedding},
            # `text` must be the non-filterable key from the index.
            # The other fields are short, so they stay filterable and a query
            # can require chunk_version without reading every old filing.
            "metadata": {
                "ticker": ticker,
                "accession": accession,
                "form": chunk["form"],
                "item": chunk["item"],
                "section": chunk["section"],
                "chunk_version": CHUNK_VERSION,
                "text": chunk["text"],
            },
        })

    # Move chunk 0 to the end of the list, then send the list in batches.
    if len(records) > 1:
        records.append(records.pop(0))

    for start in range(0, len(records), _PUT_BATCH_SIZE):
        s3vectors.put_vectors(
            vectorBucketName=bucket,
            indexName=index,
            vectors=records[start:start + _PUT_BATCH_SIZE],
        )


# Words in the question point at the item numbers from utils/sections.py.
# 10-K and 10-Q use different numbers for the same kind of discussion.
_SECTION_HINTS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("risk",), "1A", "1A"),
    (("market risk", "interest rate", "foreign exchange", "currency", "commodity"), "7A", "3"),
    (("debt", "borrowing", "commitment", "contingenc", "lease"), "8", "1"),
    (("segment",), "8", "1"),
    (("revenue", "sales", "margin", "expense", "income", "growth", "driver", "liquidity", "cash", "capital"), "7", "2"),
    (("business", "product", "competition", "strategy", "customer"), "1", ""),
)


def _items_for_query(form: str, query: str) -> list[str]:
    """Pick the filing items worth searching for this question.

    A revenue question searches MD&A (10-K Item 7, 10-Q Item 2) and, because
    the word revenue is also in the hint for the notes, the financial-statement
    item. A question with no recognized words searches the whole filing.
    """
    text = query.lower()
    items: list[str] = []
    for words, item_10k, item_10q in _SECTION_HINTS:
        if not any(word in text for word in words):
            continue
        item = item_10k if form == "10-K" else item_10q
        if item and item not in items:
            items.append(item)
    # Revenue and segment questions should see both the discussion and the notes.
    if any(word in text for word in ("revenue", "sales", "segment")):
        discussion = "7" if form == "10-K" else "2"
        notes = "8" if form == "10-K" else "1"
        for item in (discussion, notes):
            if item not in items:
                items.append(item)
    return items


@dataclass(frozen=True)
class _Hit:
    """One returned chunk. A smaller distance is a closer meaning."""

    key: str
    distance: float
    text: str


def _chunk_body(text: str) -> str:
    """The passage without the [10-Q Part I, Item 2 | ...] label on the first line."""
    label, _, body = text.partition("\n")
    if label.startswith("[") and label.endswith("]") and body.strip():
        return " ".join(body.split())
    return " ".join(text.split())


def _dedupe(hits: list[_Hit]) -> list[_Hit]:
    """Keep the closest copy when two searches return the same or overlapping text.

    Neighboring chunks share 50 words, so the start of a later chunk often
    sits inside an earlier one. The closer hit is kept because the list is
    sorted by distance first.
    """
    ordered = sorted(hits, key=lambda hit: hit.distance)
    kept: list[_Hit] = []
    seen_keys: set[str] = set()
    for hit in ordered:
        if not hit.key or hit.key in seen_keys:
            continue
        body = _chunk_body(hit.text)
        snippet = body[:160]
        overlaps = False
        for previous in kept:
            previous_body = _chunk_body(previous.text)
            if snippet and (snippet in previous_body or previous_body[:160] in body):
                overlaps = True
                break
        if overlaps:
            continue
        seen_keys.add(hit.key)
        kept.append(hit)
    return kept


def _query(
    s3vectors,
    bucket: str,
    index: str,
    accession: str,
    query_embedding: list[float],
    max_results: int,
    item: str | None = None,
) -> list[_Hit]:
    """Return the closest stored passages for this accession.

    The filter runs before the similarity search, so passages from other
    filings in the same index cannot come back. Passing `item` limits the
    search to one section, such as Item 7. `returnMetadata` is what includes
    the chunk text. That call also needs the GetVectors permission.
    """
    clauses: list[dict[str, str]] = [
        {"accession": accession},
        {"chunk_version": CHUNK_VERSION},
    ]
    if item:
        clauses.append({"item": item})
    response = s3vectors.query_vectors(
        vectorBucketName=bucket,
        indexName=index,
        queryVector={"float32": query_embedding},
        topK=max_results,
        filter={"$and": clauses},
        returnDistance=True,
        returnMetadata=True,
    )
    hits: list[_Hit] = []
    for row in response.get("vectors") or []:
        metadata = row.get("metadata") or {}
        text = metadata.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        distance = row.get("distance")
        hits.append(_Hit(
            key=str(row.get("key") or ""),
            distance=float(distance) if isinstance(distance, (int, float)) else 1.0,
            text=text.strip(),
        ))
    if not hits and (response.get("vectors") or []):
        raise VectorSearchError(
            "The index returned matches with no `text` metadata. "
            "Recreate filing-chunks with a non-filterable metadata key named text."
        )
    return hits


def search_filing_text(
    ticker: str,
    accession: str,
    query: str,
    text: str,
    form: str,
    max_results: int = 5,
) -> list[str]:
    """Embed `query` and return the closest passages from this filing's text.

    The first call for an accession chunks `text`, embeds it, and stores it.
    Every later call only embeds the question and queries the index.
    """
    query = query.strip()
    if not query:
        return []
    # A handful of passages is enough for one citation. A huge topK would
    # paste most of the filing back into the model call.
    max_results = min(max(max_results, 1), 8)

    bucket, index, model_id, _region = _settings()
    openai_client, s3vectors = _clients()

    try:
        if not _already_stored(s3vectors, bucket, index, accession):
            chunks = _section_chunks(form, text)
            if not chunks:
                return ["ERROR: the filing text was empty, so there is nothing to search."]
            # stderr, not stdout: the MCP server speaks JSON on stdout.
            print(
                f"Indexing {ticker} {accession} ({form}): {len(chunks)} section chunks",
                file=sys.stderr,
            )
            embeddings = _embed_all(openai_client, model_id, [chunk["text"] for chunk in chunks])
            _put_chunks(s3vectors, bucket, index, ticker, accession, chunks, embeddings)

        query_embedding = _embed_all(openai_client, model_id, [query])[0]
        # Search each likely item on its own so MD&A and the notes can both
        # contribute a passage. If none of those items match, search the filing.
        items = _items_for_query(form, query)
        hits: list[_Hit] = []
        for item in items:
            hits.extend(_query(
                s3vectors, bucket, index, accession, query_embedding, max_results, item,
            ))
        if not hits:
            hits = _query(s3vectors, bucket, index, accession, query_embedding, max_results)
        print(
            f"Searched {ticker} {accession} items {items or ['all']}: {len(hits)} hits",
            file=sys.stderr,
        )
        return [hit.text for hit in _dedupe(hits)[:max_results]]
    except VectorSearchError:
        raise
    except OpenAIError as exc:
        raise VectorSearchError(f"OpenAI embeddings failed: {exc}") from exc
    except (ClientError, BotoCoreError) as exc:
        raise VectorSearchError(_aws_message(exc)) from exc
