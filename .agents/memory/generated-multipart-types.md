---
name: Generated multipart types
description: TypeScript library configuration needed for generated binary upload request types.
---

OpenAPI binary upload schemas generate `Blob` and `File` types in the API client and Zod packages. Keep DOM libraries enabled for `lib/api-zod` when the API contract includes multipart file uploads.

**Why:** Orval's generated file schema failed the composite library typecheck when the package only had non-browser TypeScript libraries.

**How to apply:** When adding a multipart binary endpoint, retain the DOM libraries and rerun `pnpm --filter @workspace/api-spec run codegen`.
