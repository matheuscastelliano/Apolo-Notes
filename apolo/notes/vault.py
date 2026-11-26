"""O vault do Obsidian em volta da pasta das notas: projetos e tags.

Duas coisas que o resumo usa para encaixar a nota no vault:

  - Projetos: as subpastas de `projects_dir` (ex.: "02 PROJETOS 🏆/Vertis").
    O modelo escolhe entre elas; a nota ganha `projects: ["[[Vertis]]"]` e
    vai para "<projects_dir>/Vertis/Apolo Notes".
  - Tags: as que ja existem no frontmatter das notas do vault. Em `tags` so
    entram essas (com a grafia do vault, ex.: "SmartOSS"); as palavras-chave
    livres que o modelo inventar vao para `apolo_tags`, para nao poluir a
    lista de tags do Obsidian.

O vault e a pasta com `.obsidian`, procurada a partir da pasta das notas.
Sem vault, nada disso liga e a nota sai como antes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# Onde a nota fica dentro da pasta do projeto.
PROJECT_NOTES_FOLDER = "Apolo Notes"
# Pastas que nao sao notas de ninguem.
_SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules"}
# So o comeco do arquivo: o frontmatter fica la.
_HEAD_BYTES = 8192
# Tags que o proprio Apolo punha antes (meeting, preset...) nao sao do vault.
_TAG_CHARS = re.compile(r"^[\w/-]+$")


# Quantas tags/pessoas de cada projeto vao de pista para o modelo.
_HINT_ITEMS = 8


@dataclass
class VaultContext:
    projects: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)  # grafia do vault
    # Projeto -> "tags: vertis, SmartOSS; pessoas: Marcos, Gustavo". Sem isso
    # o modelo nao sabe que uma reuniao sobre o OSS e da Vertis (26/11).
    hints: dict[str, str] = field(default_factory=dict)

    def match_tags(self, tags: Iterable[str]) -> tuple[list[str], list[str]]:
        """(as que existem no vault, com a grafia dele; as outras)."""
        known = {tag.casefold(): tag for tag in self.tags}
        found: list[str] = []
        rest: list[str] = []
        for tag in tags:
            tag = str(tag).strip().lstrip("#")
            if not tag:
                continue
            hit = known.get(tag.casefold())
            target, value = (found, hit) if hit else (rest, tag)
            if value not in target:
                target.append(value)
        return found, rest

    def match_project(self, name: str) -> str | None:
        """O projeto com esse nome (sem diferenciar maiusculas), ou None."""
        name = name.strip().strip("[]").strip()
        return next((p for p in self.projects if p.casefold() == name.casefold()), None)


def find_vault_root(start: Path) -> Path | None:
    """A pasta com `.obsidian` em `start` ou acima dela."""
    path = Path(start).expanduser()
    for candidate in (path, *path.parents):
        if (candidate / ".obsidian").is_dir():
            return candidate
    return None


def list_projects(projects_dir: Path | None) -> list[str]:
    if not projects_dir:
        return []
    try:
        entries = sorted(Path(projects_dir).iterdir(), key=lambda p: p.name.casefold())
    except OSError as exc:
        log.warning("Projetos: nao consegui ler %s: %s", projects_dir, exc)
        return []
    return [p.name for p in entries if p.is_dir() and not p.name.startswith((".", "_"))]


def frontmatter_lists(text: str, keys: Iterable[str]) -> dict[str, list[str]]:
    """Valores das `keys` no frontmatter: `[a, b]`, `a` ou lista em bloco."""
    wanted = set(keys)
    out: dict[str, list[str]] = {k: [] for k in wanted}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return out
    current: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        indented = line.startswith((" ", "\t"))
        if current is not None and indented:
            item = re.match(r"^\s*-\s*(.+?)\s*$", line)
            if item:
                out[current].append(item.group(1))
            continue
        current = None
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or indented or key not in wanted:
            continue
        value = value.strip()
        if value.startswith("[") and value.endswith("]") and not value.startswith("[["):
            out[key] += re.findall(r'"[^"]*"|\'[^\']*\'|[^,]+', value[1:-1])
        elif value:
            out[key].append(value)
        else:
            current = key
    return {k: [v.strip().strip("'\"").strip() for v in vs if v.strip().strip("'\"").strip()]
            for k, vs in out.items()}


def frontmatter_tags(text: str) -> list[str]:
    """As tags do frontmatter: `tags: [a, b]`, `tags: a b` ou lista em bloco."""
    raw: list[str] = []
    for value in frontmatter_lists(text, ["tags"])["tags"]:
        raw += value.replace(",", " ").split()
    out = []
    for tag in raw:
        tag = tag.strip().strip("'\"").lstrip("#").strip()
        if tag and _TAG_CHARS.match(tag) and tag not in out:
            out.append(tag)
    return out


def _link_target(value: str) -> str:
    """'"[[Vertis|a Vertis]]"' -> 'Vertis'."""
    value = value.strip().strip("'\"").strip()
    if value.startswith("[[") and value.endswith("]]"):
        value = value[2:-2]
    return value.split("|", 1)[0].split("#", 1)[0].strip()


def _apolo_generic_tags() -> set[str]:
    """'meeting'/'reuniao': o Apolo ja poe em `apolo_tags`, nao e assunto."""
    from .. import i18n

    return {s["note.tag"].casefold() for s in i18n._TRANSLATIONS.values() if "note.tag" in s}


def scan_vault(
    root: Path, skip: Iterable[Path] = (), projects_dir: Path | None = None
) -> tuple[list[str], dict[str, str]]:
    """(tags do vault, pistas por projeto).

    Tags: as do frontmatter de todas as notas, menos as das pastas em `skip`
    e das "Apolo Notes" (as notas do proprio Apolo tem as tags antigas dele).
    Pistas: para cada projeto, as tags e as pessoas (`participants`) mais
    comuns nas notas da pasta dele ou com `projects: [[ele]]`.
    """
    skipped = [os.path.normcase(str(Path(p))) for p in skip]
    projects = list_projects(projects_dir)
    by_name = {p.casefold(): p for p in projects}
    base = os.path.normcase(str(projects_dir)) if projects_dir else None
    generic = _apolo_generic_tags()
    seen: dict[str, str] = {}
    tag_count: dict[str, Counter] = {p: Counter() for p in projects}
    people: dict[str, Counter] = {p: Counter() for p in projects}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        here = os.path.normcase(dirpath)
        own = (any(here == s or here.startswith(s + os.sep) for s in skipped)
               or PROJECT_NOTES_FOLDER in Path(dirpath).parts)
        folder_project = None
        if base and (here == base or here.startswith(base + os.sep)):
            first = os.path.relpath(dirpath, projects_dir).split(os.sep)[0]
            folder_project = by_name.get(first.casefold())
        for name in filenames:
            if not name.endswith(".md"):
                continue
            try:
                with open(os.path.join(dirpath, name), "rb") as f:
                    head = f.read(_HEAD_BYTES).decode("utf-8", errors="ignore")
            except OSError:
                continue
            tags = [t for t in frontmatter_tags(head) if t.casefold() not in generic]
            if not own:
                for tag in tags:
                    seen.setdefault(tag.casefold(), tag)
            fields = frontmatter_lists(head, ["participants", "projects"])
            mine = {folder_project} if folder_project else set()
            for link in fields["projects"]:
                hit = by_name.get(_link_target(link).casefold())
                if hit:
                    mine.add(hit)
            for project in mine:
                tag_count[project].update(t for t in tags if t.casefold() in seen or not own)
                people[project].update(_link_target(p) for p in fields["participants"])
    hints = {}
    for project in projects:
        parts = []
        top_tags = [t for t, _ in tag_count[project].most_common(_HINT_ITEMS)]
        top_people = [p for p, _ in people[project].most_common(_HINT_ITEMS) if p]
        if top_tags:
            parts.append("tags: " + ", ".join(top_tags))
        if top_people:
            parts.append("pessoas: " + ", ".join(top_people))
        if parts:
            hints[project] = "; ".join(parts)
    return sorted(seen.values(), key=str.casefold), hints


def scan_tags(root: Path, skip: Iterable[Path] = ()) -> list[str]:
    return scan_vault(root, skip)[0]


def load_context(notes_dir: Path, projects_dir: Path | None) -> VaultContext | None:
    """None = a pasta das notas nao esta num vault do Obsidian."""
    root = find_vault_root(notes_dir)
    if root is None:
        return None
    try:
        tags, hints = scan_vault(root, skip=[notes_dir], projects_dir=projects_dir)
    except OSError as exc:
        log.warning("Tags do vault: %s", exc)
        tags, hints = [], {}
    return VaultContext(projects=list_projects(projects_dir), tags=tags, hints=hints)


def guess_projects_dir(vault_root: Path) -> Path | None:
    """Uma pasta com "projeto"/"project" no nome, ate 2 niveis abaixo do vault."""
    level = [vault_root]
    for _ in range(2):
        nxt = []
        for folder in level:
            try:
                children = sorted((p for p in folder.iterdir() if p.is_dir()),
                                  key=lambda p: p.name.casefold())
            except OSError:
                continue
            for child in children:
                if child.name.startswith(".") or child.name in _SKIP_DIRS:
                    continue
                name = child.name.casefold()
                if "projeto" in name or "project" in name:
                    return child
                nxt.append(child)
        level = nxt
    return None


def start_scan(config: dict) -> "VaultScan | None":
    """Comeca a ler o vault (thread). None = desligado ou sem vault."""
    from .. import config as config_module

    if not config.get("organize_in_vault", True):
        return None
    notes = config_module.notes_dir(config)
    root = find_vault_root(notes)
    if root is None:
        return None
    raw = str(config.get("projects_dir") or "").strip()
    projects = Path(os.path.expandvars(raw)).expanduser() if raw else guess_projects_dir(root)
    if projects is not None and not projects.is_dir():
        log.warning("Projetos: pasta %s nao existe", projects)
        projects = None
    cache = config_module.config_dir() / "vault_tags.json"
    return VaultScan(notes, projects, cache).start()


def project_folder(projects_dir: Path, project: str) -> Path:
    return Path(projects_dir) / project / PROJECT_NOTES_FOLDER


# --------------------------------------------------------------- cache
#
# Ler o frontmatter de ~2.400 notas leva 0,5 s com o disco quente, mas levou
# 47 s na primeira vez (disco frio). Por isso a leitura comeca junto com a
# gravacao, numa thread, e o resultado fica guardado em disco: no fim da
# reuniao, se a leitura ainda nao acabou, vale a ultima lista guardada.


class VaultScan:
    def __init__(self, notes_dir: Path, projects_dir: Path | None, cache_file: Path | None):
        self.notes_dir = Path(notes_dir)
        self.projects_dir = Path(projects_dir) if projects_dir else None
        self.cache_file = cache_file
        self._result: VaultContext | None = None
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "VaultScan":
        self._thread = threading.Thread(target=self._run, name="vault-scan", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self._result = load_context(self.notes_dir, self.projects_dir)
            if self._result is not None:
                self._save_cache(self._result)
        except Exception:
            log.exception("Vault: falha ao ler as tags")
        finally:
            self._done.set()

    def result(self, wait: float = 5.0) -> VaultContext | None:
        """O contexto do vault; se a leitura nao acabou, as tags guardadas."""
        if self._thread is not None:
            self._done.wait(wait)
        if self._done.is_set():
            return self._result
        if find_vault_root(self.notes_dir) is None:
            return None
        log.info("Vault: leitura das tags ainda rodando; usando a lista guardada.")
        tags, hints = self._load_cache()
        projects = list_projects(self.projects_dir)
        return VaultContext(projects=projects, tags=tags,
                            hints={p: h for p, h in hints.items() if p in projects})

    def _save_cache(self, ctx: VaultContext) -> None:
        if self.cache_file is None:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            data = {"tags": ctx.tags, "hints": ctx.hints}
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_file)
        except OSError as exc:
            log.warning("Vault: nao consegui guardar as tags: %s", exc)

    def _load_cache(self) -> tuple[list[str], dict[str, str]]:
        if self.cache_file is None:
            return [], {}
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            hints = data.get("hints") or {}
            return ([str(t) for t in data.get("tags", [])],
                    {str(k): str(v) for k, v in hints.items()})
        except (OSError, ValueError, AttributeError):
            return [], {}
