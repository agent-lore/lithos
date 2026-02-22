"""Knowledge graph - NetworkX wiki-link graph operations."""

import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import networkx as nx

from exogram.config import ExogramConfig, get_config
from exogram.knowledge import KnowledgeDocument


@dataclass
class LinkedDocument:
    """A linked document reference."""

    id: str
    title: str


@dataclass
class LinkInfo:
    """Information about links for a document."""

    outgoing: list[LinkedDocument]
    incoming: list[LinkedDocument]


class KnowledgeGraph:
    """NetworkX-based knowledge graph for wiki-links."""

    def __init__(self, config: ExogramConfig | None = None):
        """Initialize knowledge graph.

        Args:
            config: Configuration. Uses global config if not provided.
        """
        self._config = config
        self._graph: nx.DiGraph | None = None
        # Lookup tables for link resolution
        self._id_to_node: dict[str, str] = {}  # doc_id -> node_id
        self._path_to_node: dict[str, str] = {}  # relative_path -> node_id
        self._filename_to_nodes: dict[str, list[str]] = {}  # filename -> [node_ids]
        self._alias_to_node: dict[str, str] = {}  # alias -> node_id

    @property
    def config(self) -> ExogramConfig:
        """Get configuration."""
        return self._config or get_config()

    @property
    def graph_cache_path(self) -> Path:
        """Get path to graph cache file."""
        return self.config.storage.graph_path / "graph.pickle"

    @property
    def graph(self) -> nx.DiGraph:
        """Get graph, creating if needed."""
        if self._graph is None:
            self._graph = nx.DiGraph()
        return self._graph

    def load_cache(self) -> bool:
        """Load graph from cache.

        Returns:
            True if cache was loaded, False otherwise
        """
        cache_path = self.graph_cache_path
        if not cache_path.exists():
            return False

        try:
            with open(cache_path, "rb") as f:
                data = pickle.load(f)
                self._graph = data.get("graph", nx.DiGraph())
                self._id_to_node = data.get("id_to_node", {})
                self._path_to_node = data.get("path_to_node", {})
                self._filename_to_nodes = data.get("filename_to_nodes", {})
                self._alias_to_node = data.get("alias_to_node", {})
            return True
        except Exception:
            return False

    def save_cache(self) -> None:
        """Save graph to cache."""
        cache_path = self.graph_cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "graph": self._graph,
            "id_to_node": self._id_to_node,
            "path_to_node": self._path_to_node,
            "filename_to_nodes": self._filename_to_nodes,
            "alias_to_node": self._alias_to_node,
        }

        with open(cache_path, "wb") as f:
            pickle.dump(data, f)

    def add_document(self, doc: KnowledgeDocument) -> None:
        """Add or update a document in the graph.

        Args:
            doc: Document to add
        """
        node_id = doc.id

        # Remove existing node if present (to update)
        if node_id in self.graph:
            self._remove_node_lookups(node_id)
            self.graph.remove_node(node_id)

        # Add node with attributes
        self.graph.add_node(
            node_id,
            title=doc.title,
            path=str(doc.path),
            aliases=doc.metadata.aliases,
        )

        # Update lookup tables
        self._id_to_node[doc.id] = node_id
        self._path_to_node[str(doc.path)] = node_id

        # Filename lookup (without extension)
        filename = doc.path.stem
        if filename not in self._filename_to_nodes:
            self._filename_to_nodes[filename] = []
        if node_id not in self._filename_to_nodes[filename]:
            self._filename_to_nodes[filename].append(node_id)

        # Alias lookups
        for alias in doc.metadata.aliases:
            self._alias_to_node[alias.lower()] = node_id

        # Add edges for wiki-links
        for link in doc.links:
            target_node = self._resolve_link(link.target)
            if target_node:
                self.graph.add_edge(node_id, target_node, link_text=link.target)
            else:
                # Store unresolved link as edge to placeholder
                placeholder = f"__unresolved__{link.target}"
                if placeholder not in self.graph:
                    self.graph.add_node(placeholder, unresolved=True, link_text=link.target)
                self.graph.add_edge(node_id, placeholder, link_text=link.target)

        # Resolve any previously unresolved links that now point to this document
        self._resolve_pending_links(doc)

    def _remove_node_lookups(self, node_id: str) -> None:
        """Remove a node from lookup tables."""
        # Remove from id lookup
        for doc_id, nid in list(self._id_to_node.items()):
            if nid == node_id:
                del self._id_to_node[doc_id]
                break

        # Remove from path lookup
        for path, nid in list(self._path_to_node.items()):
            if nid == node_id:
                del self._path_to_node[path]
                break

        # Remove from filename lookup
        for filename, nodes in list(self._filename_to_nodes.items()):
            if node_id in nodes:
                nodes.remove(node_id)
                if not nodes:
                    del self._filename_to_nodes[filename]
                break

        # Remove from alias lookup
        for alias, nid in list(self._alias_to_node.items()):
            if nid == node_id:
                del self._alias_to_node[alias]

    def _resolve_pending_links(self, doc: KnowledgeDocument) -> None:
        """Resolve any unresolved links that now point to this document.

        Args:
            doc: The newly added document
        """
        # Possible targets that could match this document
        possible_targets = [
            doc.path.stem,  # filename without extension
            str(doc.path),  # full path
            doc.id,  # document ID
            doc.title.lower().replace(" ", "-"),  # slugified title
        ]
        # Add aliases
        possible_targets.extend([a.lower() for a in doc.metadata.aliases])

        # Find matching unresolved placeholders
        placeholders_to_resolve = []
        for node in list(self.graph.nodes()):
            if node.startswith("__unresolved__"):
                link_text = node.replace("__unresolved__", "")
                # Check if this unresolved link matches our document
                if link_text.lower() in [
                    t.lower() for t in possible_targets
                ] or link_text.lower().replace(" ", "-") in [t.lower() for t in possible_targets]:
                    placeholders_to_resolve.append((node, link_text))

        # Resolve each placeholder
        for placeholder, _link_text in placeholders_to_resolve:
            # Get all edges pointing to this placeholder
            predecessors = list(self.graph.predecessors(placeholder))

            # Redirect edges to the real node
            for pred in predecessors:
                edge_data = self.graph.edges[pred, placeholder]
                self.graph.remove_edge(pred, placeholder)
                self.graph.add_edge(pred, doc.id, **edge_data)

            # Remove the placeholder node
            self.graph.remove_node(placeholder)

    def remove_document(self, doc_id: str) -> None:
        """Remove a document from the graph.

        Args:
            doc_id: Document ID to remove
        """
        node_id = self._id_to_node.get(doc_id)
        if node_id and node_id in self.graph:
            self._remove_node_lookups(node_id)
            self.graph.remove_node(node_id)

    def _resolve_link(self, target: str) -> str | None:
        """Resolve a wiki-link target to a node ID.

        Resolution precedence:
        1. Exact path: [[folder/note]] -> folder/note.md
        2. Filename: [[note]] -> */note.md (error if ambiguous)
        3. UUID: [[uuid]] -> file with that id
        4. Alias: [[alias]] -> file with that alias

        Args:
            target: Link target string

        Returns:
            Node ID if resolved, None otherwise
        """
        # 1. Exact path (with or without .md extension)
        path_target = target if target.endswith(".md") else f"{target}.md"
        if path_target in self._path_to_node:
            return self._path_to_node[path_target]

        # Also try without .md for paths that might include it
        if target in self._path_to_node:
            return self._path_to_node[target]

        # 2. Filename match
        filename = target.split("/")[-1]  # Get last component
        if filename.endswith(".md"):
            filename = filename[:-3]
        if filename in self._filename_to_nodes:
            nodes = self._filename_to_nodes[filename]
            if len(nodes) == 1:
                return nodes[0]
            # Ambiguous - return None (could raise error)
            return None

        # 3. UUID match
        if target in self._id_to_node:
            return self._id_to_node[target]

        # 4. Alias match
        if target.lower() in self._alias_to_node:
            return self._alias_to_node[target.lower()]

        return None

    def get_links(
        self,
        doc_id: str,
        direction: Literal["outgoing", "incoming", "both"] = "both",
        depth: int = 1,
    ) -> LinkInfo:
        """Get links for a document.

        Args:
            doc_id: Document ID
            direction: Link direction to retrieve
            depth: Traversal depth (1-3)

        Returns:
            LinkInfo with outgoing and incoming links
        """
        depth = max(1, min(3, depth))  # Clamp to 1-3
        node_id = self._id_to_node.get(doc_id)

        if not node_id or node_id not in self.graph:
            return LinkInfo(outgoing=[], incoming=[])

        outgoing: list[LinkedDocument] = []
        incoming: list[LinkedDocument] = []

        if direction in ("outgoing", "both"):
            outgoing = self._get_reachable_nodes(node_id, depth, forward=True)

        if direction in ("incoming", "both"):
            incoming = self._get_reachable_nodes(node_id, depth, forward=False)

        return LinkInfo(outgoing=outgoing, incoming=incoming)

    def _get_reachable_nodes(
        self,
        start_node: str,
        depth: int,
        forward: bool = True,
    ) -> list[LinkedDocument]:
        """Get all reachable nodes within depth.

        Args:
            start_node: Starting node ID
            depth: Maximum traversal depth
            forward: True for outgoing, False for incoming

        Returns:
            List of linked documents (deduplicated)
        """
        visited: set[str] = {start_node}
        current_level: set[str] = {start_node}
        result: list[LinkedDocument] = []

        for _ in range(depth):
            next_level: set[str] = set()
            for node in current_level:
                if forward:
                    neighbors = self.graph.successors(node)
                else:
                    neighbors = self.graph.predecessors(node)

                for neighbor in neighbors:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_level.add(neighbor)

                        # Skip unresolved placeholder nodes
                        if neighbor.startswith("__unresolved__"):
                            continue

                        node_data = self.graph.nodes.get(neighbor, {})
                        if not node_data.get("unresolved"):
                            result.append(
                                LinkedDocument(
                                    id=neighbor,
                                    title=node_data.get("title", ""),
                                )
                            )

            current_level = next_level
            if not current_level:
                break

        return result

    def get_broken_links(self) -> list[tuple[str, str, str]]:
        """Get all broken/unresolved links.

        Returns:
            List of (source_id, source_title, link_target) tuples
        """
        broken: list[tuple[str, str, str]] = []

        for node in self.graph.nodes():
            if node.startswith("__unresolved__"):
                continue

            node_data = self.graph.nodes[node]
            for _, target, edge_data in self.graph.out_edges(node, data=True):
                if target.startswith("__unresolved__"):
                    broken.append(
                        (
                            node,
                            node_data.get("title", ""),
                            edge_data.get("link_text", target.replace("__unresolved__", "")),
                        )
                    )

        return broken

    def get_ambiguous_links(self) -> list[tuple[str, list[str]]]:
        """Get all ambiguous link targets.

        Returns:
            List of (filename, [matching_paths]) tuples
        """
        ambiguous: list[tuple[str, list[str]]] = []

        for filename, nodes in self._filename_to_nodes.items():
            if len(nodes) > 1:
                paths = [self.graph.nodes[n].get("path", n) for n in nodes if n in self.graph.nodes]
                ambiguous.append((filename, paths))

        return ambiguous

    def clear(self) -> None:
        """Clear the graph."""
        self._graph = nx.DiGraph()
        self._id_to_node.clear()
        self._path_to_node.clear()
        self._filename_to_nodes.clear()
        self._alias_to_node.clear()

    def node_count(self) -> int:
        """Get number of document nodes (excluding unresolved)."""
        return sum(1 for n in self.graph.nodes() if not n.startswith("__unresolved__"))

    def edge_count(self) -> int:
        """Get number of edges."""
        return self.graph.number_of_edges()

    def has_node(self, node_id: str) -> bool:
        """Check if a node exists in the graph.

        Args:
            node_id: Node/document ID to check

        Returns:
            True if node exists, False otherwise
        """
        return node_id in self.graph

    def has_edge(self, source_id: str, target_id: str) -> bool:
        """Check if an edge exists between two nodes.

        Args:
            source_id: Source node ID
            target_id: Target node ID

        Returns:
            True if edge exists, False otherwise
        """
        return self.graph.has_edge(source_id, target_id)

    def get_outgoing_links(self, doc_id: str) -> list[dict]:
        """Get outgoing links from a document.

        Args:
            doc_id: Document ID

        Returns:
            List of linked document dicts with 'id' and 'title' keys
        """
        links = self.get_links(doc_id, direction="outgoing", depth=1).outgoing
        return [{"id": link.id, "title": link.title} for link in links]

    def get_incoming_links(self, doc_id: str) -> list[dict]:
        """Get incoming links to a document.

        Args:
            doc_id: Document ID

        Returns:
            List of linked document dicts with 'id' and 'title' keys
        """
        links = self.get_links(doc_id, direction="incoming", depth=1).incoming
        return [{"id": link.id, "title": link.title} for link in links]

    def get_neighbors(self, doc_id: str) -> list[dict]:
        """Get all neighbors (both incoming and outgoing) of a document.

        Args:
            doc_id: Document ID

        Returns:
            List of linked document dicts (deduplicated)
        """
        link_info = self.get_links(doc_id, direction="both", depth=1)
        # Deduplicate by ID
        seen: set[str] = set()
        result: list[dict] = []
        for doc in link_info.outgoing + link_info.incoming:
            if doc.id not in seen:
                seen.add(doc.id)
                result.append({"id": doc.id, "title": doc.title})
        return result

    def find_path(self, source_id: str, target_id: str) -> list[str] | None:
        """Find shortest path between two documents.

        Args:
            source_id: Source document ID
            target_id: Target document ID

        Returns:
            List of node IDs in path, or None if no path exists
        """
        if source_id not in self.graph or target_id not in self.graph:
            return None

        try:
            path = nx.shortest_path(self.graph, source_id, target_id)
            return list(path)
        except nx.NetworkXNoPath:
            return None

    def find_orphans(self) -> list[str]:
        """Find documents with no incoming or outgoing links.

        Returns:
            List of orphan document IDs
        """
        orphans: list[str] = []

        for node in self.graph.nodes():
            # Skip unresolved placeholder nodes
            if node.startswith("__unresolved__"):
                continue

            # Check if node has any edges (in or out)
            in_degree = self.graph.in_degree(node)
            out_degree = self.graph.out_degree(node)

            if in_degree == 0 and out_degree == 0:
                orphans.append(node)

        return orphans

    def get_stats(self) -> dict:
        """Get graph statistics.

        Returns:
            Dictionary with graph statistics
        """
        real_nodes = [n for n in self.graph.nodes() if not n.startswith("__unresolved__")]
        unresolved_nodes = [n for n in self.graph.nodes() if n.startswith("__unresolved__")]

        return {
            "nodes": len(real_nodes),
            "edges": self.graph.number_of_edges(),
            "unresolved_links": len(unresolved_nodes),
            "orphans": len(self.find_orphans()),
            "density": nx.density(self.graph) if len(real_nodes) > 1 else 0.0,
        }

    def get_most_linked(self, limit: int = 10) -> list[dict]:
        """Get documents with most incoming links.

        Args:
            limit: Maximum number of results

        Returns:
            List of dicts with 'id', 'title', and 'incoming_count' keys, sorted by link count descending
        """
        results: list[dict] = []

        for node in self.graph.nodes():
            if node.startswith("__unresolved__"):
                continue

            in_degree = self.graph.in_degree(node)
            node_data = self.graph.nodes[node]
            results.append(
                {
                    "id": node,
                    "title": node_data.get("title", ""),
                    "incoming_count": in_degree,
                }
            )

        # Sort by incoming count descending
        results.sort(key=lambda x: x["incoming_count"], reverse=True)
        return results[:limit]

    def get_unresolved_links(self) -> list[tuple[str, str]]:
        """Get all unresolved link targets.

        Returns:
            List of (source_id, target_text) tuples
        """
        unresolved: list[tuple[str, str]] = []

        for node in self.graph.nodes():
            if node.startswith("__unresolved__"):
                continue

            for _, target, edge_data in self.graph.out_edges(node, data=True):
                if target.startswith("__unresolved__"):
                    link_text = edge_data.get("link_text", target.replace("__unresolved__", ""))
                    unresolved.append((node, link_text))

        return unresolved
