#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Single source of truth for linux-next tree definitions.

linux-next is assembled by merging ~430 subsystem trees in a fixed order.
Today that configuration is split across two hand-maintained files that must
be kept in sync by hand (and drift in practice): ``etc/control`` (the ordered
merge program + per-tree metadata) and ``etc/git-config`` (the remote URLs).

This tool keeps the configuration in ONE human-edited file -- ``trees``, a
MAINTAINERS-style stanza format -- and *generates* everything the merge
machinery consumes, so the pieces can no longer drift::

    trees --gen-control-->  etc/control        (read by do_merge / fetch_trees)
          --gen-remotes-->  etc/trees.remotes  (per-tree git [remote] stanzas)

The non-remote git plumbing (insteadOf aliases, rerere, gc, the "ours" merge
driver, ...) is *not* generated; it lives hand-maintained in
``etc/git-config.static``.  A repository's ``.git/config`` pulls both in with
git's native include mechanism::

    [include]
        path = .../etc/git-config.static    ; hand-maintained plumbing
        path = .../etc/trees.remotes         ; 100% generated

Because ``trees.remotes`` is entirely generated, regenerating it from scratch
is safe: the hand-maintained plumbing file is never touched.

Subcommands (see ``--help``):

    extract       build the initial ``trees`` from control + git-config
    gen-control   render ``etc/control`` from ``trees``
    gen-remotes   render ``etc/trees.remotes`` from ``trees``
    gen-static    extract curated ``git-config.static`` from a git-config
    validate      report drift / problems in ``trees``
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass

#: A git ``insteadOf`` shortcut as ``(alias, expansion)``,
#: e.g. ``("korg:", "https://git.kernel.org/pub/scm/linux/kernel/git/")``.
Alias = tuple[str, str]


@dataclass
class Tree:
    """One operation in the ordered linux-next merge program.

    The ``trees`` file is an ordered sequence of these; ``do_merge`` walks it
    top to bottom.  :attr:`op` selects how the entry is interpreted:

    * ``"git"`` -- an ordinary tree to fetch and merge.  Uses :attr:`name`,
      :attr:`branch`, :attr:`build`, :attr:`contacts` and (unless
      :attr:`internal`) :attr:`url`.
    * ``"switch"`` -- point the integration branch at :attr:`name`, creating
      it from :attr:`base` (or return to ``master`` when :attr:`base` is
      ``None``).  Builds the layered ``fs-current`` / ``fs-next`` sub-trees.
    * ``"branch"`` -- create/update a marker branch named :attr:`name`
      (e.g. ``pending-fixes``).
    """

    op: str
    name: str
    enabled: bool = True
    # git-only fields:
    branch: str | None = None
    build: bool = False
    contacts: str | None = None
    url: str | None = None
    internal: bool = False
    # switch-only field:
    base: str | None = None


# --------------------------------------------------------------------------- #
# git-config parsing (URL source + insteadOf expansion)
# --------------------------------------------------------------------------- #
def load_url_aliases(text: str) -> list[Alias]:
    """Parse git ``insteadOf`` shortcuts from a git-config file.

    Finds ``[url "PREFIX"]`` / ``insteadOf = ALIAS`` pairs so short URLs can
    later be expanded to their full form.

    :param text: contents of a git-config file.
    :returns: list of ``(alias, expansion)`` tuples.
    """
    aliases: list[Alias] = []
    cur: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        header = re.match(r'\[url "(.+?)"\]', stripped)
        if header:
            cur = header.group(1)
            continue
        if stripped.startswith('['):
            cur = None
            continue
        kv = re.match(r'insteadof\s*=\s*(.+?)\s*$', stripped, re.I)
        if kv and cur:
            aliases.append((kv.group(1), cur))
    return aliases


def expand_url(url: str | None, aliases: list[Alias]) -> str | None:
    """Expand a git URL shortcut to its full form.

    e.g. ``korg:tytso/ext4.git`` becomes
    ``https://git.kernel.org/pub/scm/linux/kernel/git/tytso/ext4.git``.
    URLs matching no alias (and ``None``) are returned unchanged.
    """
    if url:
        for alias, prefix in aliases:
            if url.startswith(alias):
                return prefix + url[len(alias):]
    return url


def load_remotes(text: str) -> dict[str, str]:
    """Return a ``remote name -> url`` map parsed from a git-config file.

    :param text: contents of a git-config file.
    :returns: mapping from each ``[remote "name"]`` to its raw ``url``.
    """
    urls: dict[str, str] = {}
    cur: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        header = re.match(r'\[remote "(.+?)"\]', stripped)
        if header:
            cur = header.group(1)
            continue
        if stripped.startswith('['):
            cur = None
            continue
        kv = re.match(r'url\s*=\s*(.+?)\s*$', stripped)
        if kv and cur:
            urls[cur] = kv.group(1)
    return urls


# --------------------------------------------------------------------------- #
# etc/control parsing
# --------------------------------------------------------------------------- #
def load_control(text: str, url_map: dict[str, str]) -> list[Tree]:
    """Parse a TAB-separated ``etc/control`` file into :class:`Tree` entries.

    ``control`` carries no URLs; they are looked up per tree name in
    *url_map* (typically built from git-config).  A leading ``#`` marks a
    disabled entry.

    :param text: contents of ``etc/control``.
    :param url_map: ``name -> url`` from :func:`load_remotes`.
    :returns: entries in file (merge) order.
    """
    entries: list[Tree] = []
    for raw in text.splitlines():
        if raw == '':
            continue
        enabled, line = True, raw
        if line.startswith('#'):
            enabled, line = False, line[1:]
        fields = line.split('\t')
        if len(fields) < 3:
            continue
        op = fields[1]
        if op == 'git':
            entries.append(Tree(
                op='git', contacts=fields[0], name=fields[2], branch=fields[4],
                build=(fields[5] == 'yes'), internal=(fields[3] == 'linux-next'),
                url=url_map.get(fields[2]), enabled=enabled))
        elif op == 'switch':
            base = fields[3] if len(fields) > 3 and fields[3] != '' else None
            entries.append(Tree(op='switch', name=fields[2], base=base, enabled=enabled))
        elif op == 'branch':
            entries.append(Tree(op='branch', name=fields[2], enabled=enabled))
    return entries


# --------------------------------------------------------------------------- #
# trees (MAINTAINERS-style source) <-> entries
# --------------------------------------------------------------------------- #
#: Documented header written at the top of the generated ``trees`` file.
MAINT_HEADER = """\
# linux-next tree definitions -- THE single source of truth.
#
# Generated from this file (never hand-edit the outputs):
#   etc/control        -- the merge program do_merge/fetch_trees read
#   etc/trees.remotes  -- the per-tree git [remote] stanzas (.git/config
#                         imports it via [include], next to git-config.static)
#
# One stanza per operation, separated by a blank line, processed top to
# bottom -- ORDER MATTERS (see O:). Lines starting with '#' are comments.
# Every stanza begins with N:; field tags:
#
#   N:  Name        (required) git remote name; how the tree is reported.
#   O:  Operation   (optional) absent => an ordinary tree to fetch and merge.
#                   "switch [<base>]" => point the integration branch at N:,
#                       creating it from <base> (builds fs-current/fs-next);
#                       a bare "switch" returns to the mainline master.
#                   "branch" => create/update a marker branch named N: here
#                       (e.g. pending-fixes).
#   U:  URL         (git trees) full git URL to fetch from; omitted for
#                   internal trees (I: yes).
#   B:  Branch      (git trees) remote branch at U: to fetch and merge.
#   D:  builD       (git trees) yes|no -- build right after merging this tree.
#   M:  Maintainer  (git trees) contact(s) emailed about problems; one or
#                   more comma-separated "Name <email>".
#   I:  Internal    (optional) "yes" => internal to linux-next (no remote;
#                   assembled in-tree). Has no U:/B:.
#   X:  eXcluded    (optional) "disabled" => keep the entry but skip it
#                   (emitted as a commented-out line in etc/control).
#
# Ordinary trees use N:/U:/B:/D:/M: (+ optional I:, X:); switch/branch ops
# use only N:/O:.

"""


def emit_maint(entries: list[Tree]) -> str:
    """Render *entries* as the MAINTAINERS-style ``trees`` source file."""
    blocks: list[str] = []
    for e in entries:
        lines = [f'N: {e.name}']
        if e.op == 'switch':
            lines.append('O: switch' + (f' {e.base}' if e.base else ''))
        elif e.op == 'branch':
            lines.append('O: branch')
        else:
            if e.internal:
                lines.append('I: yes')
            if e.url:
                lines.append(f'U: {e.url}')
            lines.append(f'B: {e.branch}')
            lines.append('D: ' + ('yes' if e.build else 'no'))
            lines.append(f'M: {e.contacts}')
        if not e.enabled:
            lines.append('X: disabled')
        blocks.append('\n'.join(lines))
    return MAINT_HEADER + '\n\n'.join(blocks) + '\n'


def load_maint(text: str) -> list[Tree]:
    """Parse the MAINTAINERS-style ``trees`` source file into entries.

    Stanzas are separated by blank lines; ``#`` lines are comments.  A stanza
    without an ``N:`` tag (e.g. the header block) is ignored.
    """
    entries: list[Tree] = []
    for stanza in re.split(r'\n[ \t]*\n', text):
        tags: dict[str, str] = {}
        for line in stanza.splitlines():
            line = line.rstrip()
            if not line or line.startswith('#'):
                continue
            key, _, value = line.partition(':')
            tags[key.strip()] = value.strip()
        if 'N' not in tags:
            continue
        enabled = tags.get('X') != 'disabled'
        op_field = tags.get('O', '')
        if op_field.startswith('switch'):
            parts = op_field.split(None, 1)
            entries.append(Tree(op='switch', name=tags['N'],
                                base=(parts[1] if len(parts) > 1 else None),
                                enabled=enabled))
        elif op_field.startswith('branch'):
            entries.append(Tree(op='branch', name=tags['N'], enabled=enabled))
        else:
            entries.append(Tree(
                op='git', name=tags['N'], branch=tags.get('B', ''),
                build=(tags.get('D') == 'yes'), contacts=tags.get('M', ''),
                url=tags.get('U'), internal=(tags.get('I') == 'yes'),
                enabled=enabled))
    return entries


# --------------------------------------------------------------------------- #
# generators
# --------------------------------------------------------------------------- #
def emit_control(entries: list[Tree]) -> str:
    """Render *entries* back into the TAB-separated ``etc/control`` format."""
    out: list[str] = []
    for e in entries:
        if e.op == 'git':
            col4 = 'linux-next' if e.internal else '-'
            line = '\t'.join([e.contacts or '', 'git', e.name, col4,
                              e.branch or '', 'yes' if e.build else 'no'])
        elif e.op == 'switch':
            line = '\t'.join(['-', 'switch', e.name])
            if e.base:
                line += '\t' + e.base
        else:  # branch
            line = '\t'.join(['-', 'branch', e.name])
        if not e.enabled:
            line = '#' + line
        out.append(line)
    return '\n'.join(out) + '\n'


def emit_remotes(entries: list[Tree]) -> str:
    """Render the per-tree git ``[remote]`` stanzas (the generated include).

    Internal trees and trees with no URL are skipped (they have no remote).
    """
    out = ['# GENERATED by treegen.py from "trees" -- do not edit; imported via',
           '# [include] in .git/config alongside git-config.static.', '']
    for e in entries:
        if e.op != 'git' or e.internal or not e.url:
            continue
        out += [f'[remote "{e.name}"]',
                f'\turl = {e.url}',
                '\ttagopt = --no-tags',
                f'\tfetch = +refs/heads/{e.branch}:refs/remotes/{e.name}/{e.branch}']
    return '\n'.join(out) + '\n'


#: git-config sections that are machine/user-specific and should not be
#: committed to a shared, version-controlled plumbing file.
DROP_SECTIONS = frozenset({'gui', 'maintenance', 'log', 'diff'})


def emit_static(gitconfig_text: str, drop: frozenset[str] = DROP_SECTIONS) -> str:
    """Extract the curated non-remote plumbing from a git-config file.

    Keeps every non-``[remote]`` section except the machine-specific ones in
    *drop* (window geometry, local maintenance prefs, ...).

    :param gitconfig_text: contents of the existing ``etc/git-config``.
    :param drop: section names to omit.
    :returns: the curated static config (the hand-maintained half).
    """
    kept: list[str] = []
    dropped: set[str] = set()
    keep = True
    for line in gitconfig_text.splitlines():
        if line.startswith('['):
            header = re.match(r'\[(\w[\w-]*)', line)
            name = header.group(1) if header else ''
            if line.startswith('[remote'):
                keep = False
            elif name in drop:
                keep = False
                dropped.add(name)
            else:
                keep = True
        if keep and line.strip():
            kept.append(line)
    head = ('# linux-next git plumbing -- hand-maintained, NOT generated.\n'
            '# Per-tree remotes live in trees.remotes (generated); .git/config\n'
            '# imports both via [include].\n')
    if dropped:
        head += '# (migration dropped machine-specific sections: %s)\n' \
                % ', '.join(sorted(dropped))
    return head + '\n' + '\n'.join(kept) + '\n'


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def validate(entries: list[Tree]) -> list[str]:
    """Return human-readable problems found in *entries*.

    Flags git trees with no URL (orphans), a missing branch, or a duplicated
    name.  An empty list means the definitions look internally consistent.
    """
    issues: list[str] = []
    git_names = [e.name for e in entries if e.op == 'git' and not e.internal]
    for e in entries:
        if e.op != 'git' or e.internal:
            continue
        if not e.url:
            issues.append(f"orphan tree '{e.name}' (in control, no git remote)")
        if not e.branch:
            issues.append(f"tree '{e.name}' has no branch")
    for name in sorted({n for n in git_names if git_names.count(n) > 1}):
        issues.append(f"duplicate tree name '{name}'")
    return issues


# --------------------------------------------------------------------------- #
# command-line interface
# --------------------------------------------------------------------------- #
def _read(path: str) -> str:
    """Read and return the entire contents of *path* as UTF-8 text."""
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def cmd_extract(args: argparse.Namespace) -> int:
    """``extract``: build the initial ``trees`` from control + git-config."""
    gitconfig = _read(args.git_config)
    aliases = load_url_aliases(gitconfig)
    url_map = {name: expand_url(url, aliases) or ''
               for name, url in load_remotes(gitconfig).items()}
    entries = load_control(_read(args.control), url_map)
    out_path = os.path.join(args.outdir, 'trees')
    with open(out_path, 'w', encoding='utf-8') as handle:
        handle.write(emit_maint(entries))
    n_git = sum(e.op == 'git' for e in entries)
    print(f'extracted {len(entries)} entries ({n_git} git) -> {out_path}',
          file=sys.stderr)
    return 0


def cmd_gen_control(args: argparse.Namespace) -> int:
    """``gen-control``: write ``etc/control`` to stdout."""
    sys.stdout.write(emit_control(load_maint(_read(args.trees))))
    return 0


def cmd_gen_remotes(args: argparse.Namespace) -> int:
    """``gen-remotes``: write ``etc/trees.remotes`` to stdout."""
    sys.stdout.write(emit_remotes(load_maint(_read(args.trees))))
    return 0


def cmd_gen_static(args: argparse.Namespace) -> int:
    """``gen-static``: write the curated ``git-config.static`` to stdout."""
    sys.stdout.write(emit_static(_read(args.git_config)))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """``validate``: print problems to stderr; exit non-zero if any."""
    issues = validate(load_maint(_read(args.trees)))
    for issue in issues:
        print(f'  WARN: {issue}', file=sys.stderr)
    print(f'{len(issues)} issue(s)', file=sys.stderr)
    return 1 if issues else 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse command-line parser."""
    parser = argparse.ArgumentParser(
        prog='treegen.py', description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True, metavar='COMMAND')

    p = sub.add_parser('extract',
                       help='build trees from control + git-config (migration)')
    p.add_argument('control', help='path to etc/control')
    p.add_argument('git_config', help='path to etc/git-config')
    p.add_argument('outdir', help='directory to write the trees file into')
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser('gen-control', help='render etc/control from trees')
    p.add_argument('trees', help='path to the trees source file')
    p.set_defaults(func=cmd_gen_control)

    p = sub.add_parser('gen-remotes', help='render etc/trees.remotes from trees')
    p.add_argument('trees', help='path to the trees source file')
    p.set_defaults(func=cmd_gen_remotes)

    p = sub.add_parser('gen-static',
                       help='extract curated git-config.static from a git-config')
    p.add_argument('git_config', help='path to etc/git-config')
    p.set_defaults(func=cmd_gen_static)

    p = sub.add_parser('validate',
                       help='report drift/problems in trees (exit 1 if any)')
    p.add_argument('trees', help='path to the trees source file')
    p.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the selected subcommand."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
