# Grimoire — Wiki conventions

> **Up:** [↑ Docs root](../README.md)

This tier holds the canonical wiki-doc conventions every project document
follows. Grimoire's own framework-internal design specs are **not** part of a
distributed project — they live only in the upstream Grimoire repository.

## Wiki hierarchy & relative links

This is the canonical convention authority for all wiki-doc conventions in
Grimoire. If something disagrees, this section wins.

### Breadcrumb

Every non-root, non-exempt `docs/**/*.md` opens with a blockquote breadcrumb
that **relatively links** to the nearest tier index (`README.md` in its own
directory, or a named ancestor index):

```
> **Up:** [↑ <Tier name>](<relative-path-to-README.md>)
```

### Links

- Always use **relative** links between docs (`[..](sibling.md)`,
  `[..](../features.md)`). Never use absolute paths (a leading `/` or the repo
  URL) for internal links.
- Deep-link to the specific section or leaf that answers the question
  (`[see §Design](some-design.md#design)`), not bare "see the design doc" prose.

### Tiers & indexes

- `docs/README.md` is the documentation root. Every tier below it has an index
  page (`README.md`, or a designated parent `.md`) that links each child as a
  relative link.
- Index pages stay small (≤ 6 KB) and link-dense; leaf pages stay focused.
  Load the index, follow the link, read only the relevant leaf.

### docs-map

`docs/README.md` carries an auto-generated map between
`<!-- docs-map:begin -->` … `<!-- docs-map:end -->` markers. Regenerate it with
`python3 .claude/skills/grm-doc-assurance/doc_assurance.py docs-map --write-map`;
curated content outside the markers is preserved.
