from __future__ import annotations

import csv
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from jarvis.runtime.contracts import PermissionLevel, ToolSpec
from jarvis.runtime.tooling import RuntimeTool, ToolRegistry


@dataclass(frozen=True)
class FileInsight:
    path: str
    name: str
    extension: str
    category: str
    size_bytes: int
    text: str
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "extension": self.extension,
            "category": self.category,
            "size_bytes": self.size_bytes,
            "text": self.text,
            "summary": self.summary,
            "metadata": self.metadata,
        }


class FileIntelligence:
    """Read, classify, summarize, search, compare, and organize local files."""

    def __init__(self, provider: Any | None = None, *, max_chars: int = 40_000) -> None:
        self.provider = provider
        self.max_chars = max(1_000, max_chars)

    def classify(self, path: Path) -> str:
        extension = path.suffix.lower()
        if extension in {".txt", ".md", ".log", ".json", ".yaml", ".yml"}:
            return "text"
        if extension in {".csv", ".tsv"}:
            return "table"
        if extension == ".pdf":
            return "pdf"
        if extension == ".docx":
            return "document"
        if extension == ".xlsx":
            return "spreadsheet"
        if extension == ".pptx":
            return "presentation"
        if extension in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            return "image"
        return "binary"

    async def inspect(self, path: Path) -> FileInsight:
        resolved = path.expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError(f"File not found: {resolved}")
        category = self.classify(resolved)
        text, metadata = await self._read_text(resolved, category)
        text = text[: self.max_chars]
        summary = await self._summarize(resolved, text, category)
        return FileInsight(
            path=str(resolved),
            name=resolved.name,
            extension=resolved.suffix.lower(),
            category=category,
            size_bytes=resolved.stat().st_size,
            text=text,
            summary=summary,
            metadata=metadata,
        )

    async def search(
        self,
        root: Path,
        query: str,
        *,
        limit: int = 20,
    ) -> list[FileInsight]:
        query_lower = query.lower()
        results: list[FileInsight] = []
        for path in root.expanduser().resolve().rglob("*"):
            if not path.is_file():
                continue
            if query_lower and query_lower not in path.name.lower():
                continue
            try:
                insight = await self.inspect(path)
            except Exception:
                continue
            results.append(insight)
            if len(results) >= limit:
                break
        return results

    async def compare(self, left: Path, right: Path) -> dict[str, Any]:
        left_insight = await self.inspect(left)
        right_insight = await self.inspect(right)
        left_lines = set(left_insight.text.splitlines())
        right_lines = set(right_insight.text.splitlines())
        return {
            "left": left_insight.to_dict(),
            "right": right_insight.to_dict(),
            "same_name": left_insight.name == right_insight.name,
            "same_size": left_insight.size_bytes == right_insight.size_bytes,
            "added_lines": sorted(right_lines - left_lines)[:100],
            "removed_lines": sorted(left_lines - right_lines)[:100],
        }

    def organize_plan(self, paths: list[Path], destination: Path) -> dict[str, Any]:
        categories: dict[str, list[str]] = {}
        for path in paths:
            category = self.classify(path)
            categories.setdefault(category, []).append(str(path))
        moves = [
            {
                "source": source,
                "destination": str(destination / category / Path(source).name),
                "category": category,
            }
            for category, sources in categories.items()
            for source in sources
        ]
        return {
            "requires_confirmation": True,
            "risk": "HIGH_RISK",
            "destination": str(destination),
            "moves": moves,
            "count": len(moves),
        }

    def register(self, registry: ToolRegistry) -> list[str]:
        async def inspect_path(path: str) -> dict[str, Any]:
            return (await self.inspect(Path(path))).to_dict()

        async def search_path(
            path: str,
            query: str,
            limit: int = 20,
        ) -> list[dict[str, Any]]:
            return [
                insight.to_dict()
                for insight in await self.search(Path(path), query, limit=limit)
            ]

        async def compare_paths(left: str, right: str) -> dict[str, Any]:
            return await self.compare(Path(left), Path(right))

        async def organize_paths(
            paths: list[str],
            destination: str,
        ) -> dict[str, Any]:
            return self.organize_plan(
                [Path(path) for path in paths],
                Path(destination),
            )

        definitions = [
            (
                "file_inspect",
                "Read, classify, and summarize a local file.",
                {"path": {"type": "string"}},
                ["path"],
                PermissionLevel.READ_ONLY,
                inspect_path,
            ),
            (
                "file_search",
                "Search files by name inside a directory.",
                {
                    "path": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                ["path", "query"],
                PermissionLevel.READ_ONLY,
                search_path,
            ),
            (
                "file_compare",
                "Compare two local text files.",
                {
                    "left": {"type": "string"},
                    "right": {"type": "string"},
                },
                ["left", "right"],
                PermissionLevel.READ_ONLY,
                compare_paths,
            ),
            (
                "file_organize_plan",
                "Create a confirmation-gated plan for classifying and moving files.",
                {
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "destination": {"type": "string"},
                },
                ["paths", "destination"],
                PermissionLevel.READ_ONLY,
                organize_paths,
            ),
        ]
        names: list[str] = []
        for name, description, properties, required, permission, handler in definitions:
            registry.register(
                RuntimeTool(
                    spec=ToolSpec(
                        name=name,
                        description=description,
                        parameters={
                            "type": "object",
                            "properties": properties,
                            "required": required,
                            "additionalProperties": False,
                        },
                        permission=permission,
                        capability="file",
                        category="FILE",
                        risk_level=permission,
                    ),
                    handler=handler,
                )
            )
            names.append(name)
        return names

    async def _read_text(self, path: Path, category: str) -> tuple[str, dict[str, Any]]:
        if category in {"text", "table"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            metadata: dict[str, Any] = {}
            if category == "table":
                rows = list(csv.reader(text.splitlines()))
                metadata = {
                    "rows": len(rows),
                    "columns": max((len(row) for row in rows), default=0),
                }
            return text, metadata
        if category == "pdf":
            return self._read_pdf(path)
        if category == "document":
            return self._read_docx(path)
        if category == "spreadsheet":
            return self._read_xlsx(path)
        if category == "presentation":
            return self._read_pptx(path)
        return "", {"binary": True}

    @staticmethod
    def _read_pdf(path: Path) -> tuple[str, dict[str, Any]]:
        try:
            from pypdf import PdfReader
        except ImportError:
            return "", {
                "error": "PDF text extraction requires pypdf.",
                "pages": None,
            }
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return text, {"pages": len(reader.pages)}

    @staticmethod
    def _read_docx(path: Path) -> tuple[str, dict[str, Any]]:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        text = "\n".join(
            node.text or ""
            for node in root.iter()
            if node.tag.endswith("}t")
        )
        return text, {}

    @staticmethod
    def _read_xlsx(path: Path) -> tuple[str, dict[str, Any]]:
        with zipfile.ZipFile(path) as archive:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = [
                    "".join(node.itertext())
                    for node in root
                    if node.tag.endswith("}si")
                ]
            values: list[str] = []
            sheet_names = [
                name
                for name in archive.namelist()
                if re.match(r"xl/worksheets/sheet\d+\.xml$", name)
            ]
            for sheet in sheet_names:
                root = ElementTree.fromstring(archive.read(sheet))
                for cell in root.iter():
                    if not cell.tag.endswith("}c"):
                        continue
                    value = next(
                        (node for node in cell if node.tag.endswith("}v")),
                        None,
                    )
                    if value is None or value.text is None:
                        continue
                    if cell.attrib.get("t") == "s":
                        index = int(value.text)
                        if 0 <= index < len(shared):
                            values.append(shared[index])
                    else:
                        values.append(value.text)
        return "\t".join(values), {"sheets": len(sheet_names)}

    @staticmethod
    def _read_pptx(path: Path) -> tuple[str, dict[str, Any]]:
        with zipfile.ZipFile(path) as archive:
            slides = sorted(
                name
                for name in archive.namelist()
                if re.match(r"ppt/slides/slide\d+\.xml$", name)
            )
            texts: list[str] = []
            for slide in slides:
                root = ElementTree.fromstring(archive.read(slide))
                texts.extend(
                    node.text or ""
                    for node in root.iter()
                    if node.tag.endswith("}t")
                )
        return "\n".join(texts), {"slides": len(slides)}

    async def _summarize(self, path: Path, text: str, category: str) -> str:
        if not text:
            return f"{path.name} is a {category} file."
        if self.provider is not None and len(text) > 800:
            try:
                reply = await self.provider.complete(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Summarize this file for a personal assistant. "
                                "State the purpose, important facts, risks, and action items. "
                                "Keep it concise and factual."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"File: {path.name}\n\n{text[:12_000]}",
                        },
                    ],
                    [],
                )
                if reply.content.strip():
                    return reply.content.strip()
            except Exception:
                pass
        return " ".join(text.split())[:500]
