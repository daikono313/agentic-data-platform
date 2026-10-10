# frontend（Vite + React + TypeScript）

- パッケージ管理は pnpm。UI は Tailwind + shadcn/ui。
- BEAM 成果物の型は backend の JSON Schema から生成する（`src/generated/`、手で編集しない）。
- backend との通信は REST + SSE（[design-doc の API](../docs/design-doc.md#apibackend)）。
- UI の文言は日本語。
- Canvas / Event Matrix は表示のみで、編集機能は作らない（Non-goal）。
- テストは vitest と Testing Library。
