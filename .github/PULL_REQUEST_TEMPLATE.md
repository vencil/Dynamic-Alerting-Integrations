## Summary

<!-- Brief description of the changes -->

## Type of Change

- [ ] Feature (new functionality)
- [ ] Bug fix
- [ ] Documentation
- [ ] Refactoring (no behavior change)
- [ ] CI/CD / Tooling
- [ ] Release

## Documentation Checklist

<!-- For documentation-related PRs, check applicable items -->

- [ ] Frontmatter `version` updated (`check_frontmatter_versions.py --fix`)
- [ ] Cross-language counterpart updated (ZH ↔ EN)
- [ ] No orphan documents introduced (new .md files linked from at least one other doc)
- [ ] CHANGELOG.md updated (if user-facing change)
- [ ] Numbers accurate (tool count, scenario count, Rule Pack count match source of truth)
- [ ] No codenames or jargon — descriptive names only

## Quality Gates

```bash
make version-check                                    # Version consistency
make lint-docs                                        # Documentation lint
pre-commit run --all-files                            # Auto hooks
pre-commit run --hook-stage manual --all-files        # Manual hooks (heavier)
```

## Test Plan

<!-- How was this tested? -->
