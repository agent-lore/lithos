# Research Cache — Introduction

In a multi-agent system, research is the most expensive operation: it consumes API tokens, takes time, and often produces knowledge that another agent already holds. Without a mechanism to check what is already known — and whether it is still valid — agents default to researching from scratch every time, creating duplicate notes and burning budget on redundant work. The research cache addresses this by giving agents a single, low-cost call to ask Lithos "do you already know this, and is it still fresh?" before committing to any external lookup. It introduces an explicit time-to-live field on knowledge documents so that the writing agent can declare how long its findings should be trusted — competitor pricing might be valid for a week, API documentation for a month, breaking news for a few hours. The cache lookup tool then enforces this contract: it returns a direct hit if fresh, high-confidence knowledge exists, a miss with a stale document ID if the knowledge exists but has expired (signalling the agent to refresh and update rather than duplicate), or a clean miss if nothing relevant is stored at all. The goal is not to prevent research — it is to make research a last resort rather than a reflex, and to keep the knowledge base coherent by converging on updated notes rather than accumulating stale copies.

The Gap

Right now an agent wanting to avoid duplicate research has to:

Call lithos_search or lithos_semantic
Manually inspect updated_at from the returned metadata (which isn't even in the search results — only snippet, score, path come back)
Decide if it's fresh enough
Call lithos_read to get the full doc

That's 2-3 tool calls and requires the agent to do the freshness logic itself. The cache should be one call.

Change 1 — knowledge.py: Add expires_at to KnowledgeMetadata
@dataclass
class KnowledgeMetadata:
    id: str
    title: str
    author: str
    created_at: datetime
    updated_at: datetime
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    confidence: float = 1.0
    contributors: list[str] = field(default_factory=list)
    source: str | None = None
    supersedes: str | None = None
    expires_at: datetime | None = None          # ← ADD THIS

    @property
    def is_stale(self) -> bool:                 # ← ADD THIS
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > _normalize_datetime(self.expires_at)


Also update to_dict() and from_dict() to handle expires_at (same pattern as created_at/updated_at).

Update KnowledgeManager.create() and update() to accept expires_at: datetime | None = None.

Change 2 — server.py: Extend lithos_write

Add two optional parameters — agents can use either:

async def lithos_write(
    title: str,
    content: str,
    agent: str,
    tags: list[str] | None = None,
    confidence: float = 1.0,
    path: str | None = None,
    id: str | None = None,
    source_task: str | None = None,
    ttl_hours: float | None = None,      # ← ADD: "valid for N hours from now"
    expires_at: str | None = None,       # ← ADD: explicit ISO datetime
) -> dict[str, str]:


In the body, compute expires_at_dt from whichever is provided:

expires_at_dt = None
if ttl_hours is not None:
    expires_at_dt = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
elif expires_at is not None:
    expires_at_dt = datetime.fromisoformat(expires_at)


Then pass expires_at=expires_at_dt to knowledge.create() / knowledge.update().

Usage example — an agent writing competitor pricing:

lithos_write(title="Acme Pricing", content="...", agent="az", ttl_hours=168)  # valid 7 days

Change 3 — server.py: Add lithos_cache_lookup (the key new tool)

This is the main addition. One call, returns hit/miss:

@self.mcp.tool()
async def lithos_cache_lookup(
    query: str,
    max_age_hours: float | None = None,
    min_confidence: float = 0.5,
    limit: int = 3,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Check if fresh knowledge exists before doing research.

    Call this BEFORE web search or expensive research. Returns a cache
    hit if sufficiently fresh, high-confidence knowledge already exists.

    Args:
        query: What you're about to research
        max_age_hours: Reject docs older than N hours (uses updated_at).
                       If None, only expires_at is checked.
        min_confidence: Minimum confidence score (default: 0.5)
        limit: Max candidate docs to consider (default: 3)
        tags: Restrict to tagged docs

    Returns:
        hit: bool — True if usable cached knowledge found
        document: full doc dict if hit, else None
        stale_exists: bool — True if relevant docs exist but are stale
                      (signals: re-research and update, don't create new)
        stale_id: str — ID of stale doc to update (if stale_exists)
    """
    results = self.search.semantic_search(query=query, limit=limit, tags=tags)
    
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours) if max_age_hours else None
    
    best_hit = None
    stale_candidate = None
    
    for r in results:
        doc, _ = await self.knowledge.read(id=r.id)
        meta = doc.metadata
        
        # Skip low confidence
        if meta.confidence < min_confidence:
            continue
        
        # Check explicit expiry
        if meta.is_stale:
            stale_candidate = stale_candidate or doc
            continue
        
        # Check age cutoff
        if cutoff and _normalize_datetime(meta.updated_at) < cutoff:
            stale_candidate = stale_candidate or doc
            continue
        
        best_hit = doc
        break
    
    if best_hit:
        return {
            "hit": True,
            "document": {
                "id": best_hit.id,
                "title": best_hit.title,
                "content": best_hit.content,
                "confidence": best_hit.metadata.confidence,
                "updated_at": best_hit.metadata.updated_at.isoformat(),
                "expires_at": best_hit.metadata.expires_at.isoformat() 
                              if best_hit.metadata.expires_at else None,
            },
            "stale_exists": False,
            "stale_id": None,
        }
    
    return {
        "hit": False,
        "document": None,
        "stale_exists": stale_candidate is not None,
        "stale_id": stale_candidate.id if stale_candidate else None,
    }


The stale_id return is important — it tells the agent to update the existing note rather than create a duplicate.

Change 4 — server.py: Return freshness in search results

Small but useful — add updated_at and is_stale to lithos_search and lithos_semantic results so agents can see freshness without a separate read:

# In lithos_search results:
{
    "id": r.id,
    "title": r.title,
    "snippet": r.snippet,
    "score": r.score,
    "path": r.path,
    "updated_at": r.updated_at,    # ← ADD (requires SearchResult to carry this)
    "is_stale": r.is_stale,        # ← ADD
}


This requires SearchResult in search.py to carry updated_at and expires_at — small change to the dataclass and indexing.

Summary of Files Changed
File	Change
knowledge.py	Add expires_at + is_stale to KnowledgeMetadata; update create()/update()
server.py	Add ttl_hours/expires_at to lithos_write; add lithos_cache_lookup tool; add freshness to search results
search.py	Add updated_at/expires_at to SearchResult dataclass and indexing

No schema migrations, no new dependencies, no new storage. The expires_at field is just another YAML frontmatter key — the file watcher picks it up automatically.

How Agents Use It

Before (current — 3 calls, agent does logic):

lithos_semantic(query) → lithos_read(id) → decide freshness manually


After (1 call, Lithos does logic):

lithos_cache_lookup(query, max_age_hours=24, min_confidence=0.7)
  → hit=True  → use cached content, skip research
  → hit=False, stale_exists=True, stale_id=X  → research, then lithos_write(id=X, ...)
  → hit=False, stale_exists=False  → research, then lithos_write(title=..., ttl_hours=48, ...)


The stale_id path is the key token-saving behaviour — agents update existing notes rather than creating duplicates, keeping the knowledge base clean.