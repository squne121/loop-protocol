# AGENTS.md

このリポジトリでは、`.claude/skills/**/SKILL.md` をエージェントスキルの正本とする。
git read-only probe は raw git より probe script を優先する。
repo-approved local temporary workspace は `tmp/` を正本とし、`.claude/tmp/` は非推奨の legacy root（read/scan 目的では引き続き認識される）である。変更時は `docs/dev/repository-folder-policy.md` を更新する。
