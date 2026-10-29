# infa2dbt: codebase review and production-readiness plan

## 1. Scope and executive summary

Reviewed the current application, parser and lineage code, API contracts and client, tests, dependency manifests, service/build configuration, and auxiliary workspace scaffolding. Source references below refer to the reviewed code at `cceef1e`, before this document was added.

**The current product is a PowerCenter XML mapping inspector with structural field lineage, not an XML-to-dbt converter.** It accepts XML, produces an intermediate representation (IR), orders instance dependencies, and displays graphs and upstream hops. It does not generate SQL, dbt models, YAML, a dbt project, or a downloadable migration bundle. `README.md:3–7` acknowledges the generation gap.

The implementation has useful foundations: explicit models, deterministic graph traversal, hardened XML parsing, sample/file trace support, and regression coverage. It is not yet ready to promise faithful migration semantics or unrestricted public parsing.

Two release objectives must be separated:

1. **Production inspector:** trustworthy, explicitly scoped parsing/lineage, reliable deployment, bounded resource use, and clear data-handling behavior.
2. **Production migrator:** everything above, plus a supported transformation compiler and validated dbt output.

The original no-database/no-authentication scope remains a constraint. This plan does not authorize either feature, or any implementation changes.

### Review method and limits

- No application source, dependencies, services, or configuration were changed; only this document was created. No commit was made.
- Reviewed existing tests; did not rerun the test suites or builds during this review.
- Ran read-only Node dependency audit and dependency-path queries.
- Ran small, synthetic, in-memory parser/lineage probes without writing fixtures.
- Inspected the installed multipart implementation to verify temporary-file behavior.
- This was not a penetration test, production traffic review, Python dependency advisory audit, or warehouse execution test. Platform/proxy controls not declared in this repository are unverified, not assumed absent.

## 2. Current architecture

| Area | Implementation and responsibility |
| --- | --- |
| Browser application | `artifacts/infa2dbt/src/App.tsx`: React, Vite, Tailwind, Wouter, and TanStack Query. Upload/sample selection, readiness checks, theme, and error/loading states. |
| Inspection UI | `src/components/parse-inspector.tsx` under the web artifact: mapping selector; lineage, summary, ports, and raw IR views. |
| Graph UI | `src/components/lineage/`: Dagre layout and React Flow rendering; instance details, inline target-column tracing, path highlighting, dimming, and Clear trace. |
| Active HTTP backend | `artifacts/infa2dbt/api/main.py`: FastAPI routes; upload/sample handling; calls into the standalone parser; optionally serves built frontend assets. |
| Python startup | `artifacts/infa2dbt/api/serve.py`: adds the repository root to the module path and starts Uvicorn on `PORT`. |
| Parser/IR | Root `api/parser/models.py`, `parser.py`, `order.py`, and `lineage.py`: Pydantic models, lxml parsing, stable Kahn ordering, NetworkX instance graph, and iterative field traversal. |
| API contract/client | `lib/api-spec/openapi.yaml` and Orval configuration generate React Query hooks/types and Zod schemas. `lib/api-client-react/src/custom-fetch.ts` handles requests and structured HTTP/response-parsing errors. |
| Samples | Root `samples/`; the API lists and parses allowlisted XML files. Uploaded documents are not intentionally persisted as application records. Multipart handling can nevertheless create temporary files; see security findings. |
| Tests | Root `api/tests/` covers parser, API, and lineage behavior. The five frontend lineage tests launch a real temporary FastAPI server but mock the React Flow canvas. |

### Request/data flow

```text
Browser selects XML
  -> POST /api/upload: filename/size validation and metadata
  -> POST /api/parse: sends the same file again
     -> XML -> Pydantic IR -> instance order + lineage graph
  -> inspect mappings and target columns
  -> POST /api/lineage/trace:
     resend original XML, or name a bundled sample
     -> reparse -> rebuild selected graph -> return upstream hops

Bundled sample
  -> POST /api/samples/{filename}/parse
  -> same parsing/inspection pipeline
```

The API also exposes unprefixed aliases. `/health`, `/healthz`, and their `/api` aliases all return the same constant readiness response.

### Development and production services

- The web artifact's development service runs Vite on port 24518.
- The artifact named API Server runs **Python**, not the adjacent Express application, on port 8080.
- Both product artifacts declare production runs of `python artifacts/infa2dbt/api/serve.py`, on their respective ports. Their route declarations cover `/` and `/api`.
- FastAPI serves `artifacts/infa2dbt/dist/public` only if that directory exists when the application is imported (`main.py:153–154`).
- The API artifact's build still builds the Node scaffold, while its run command starts Python. Neither product artifact's build command explicitly installs Python dependencies. Actual platform dependency provisioning must be verified in a clean release environment.

Evidence: `artifacts/{infa2dbt,api-server}/.replit-artifact/artifact.toml`. Do not infer that the Express health-only router is the active product API merely from its directory name.

### Auxiliary and unused scaffolding

- `artifacts/api-server/src/` contains a separate Express health server with permissive CORS and Pino logging. Those middleware settings do **not** protect or configure the active FastAPI application.
- `lib/db/` contains a PostgreSQL/Drizzle client and an empty schema; the product request path does not use it.
- However, `.replit:18–20` registers `scripts/post-merge.sh`, which runs dependency installation and a DB push. This conflicts with the database-free application scope and can break setup or attempt an unwanted schema operation if invoked in an environment with a database.
- `artifacts/mockup-sandbox/` is the design/component-preview scaffold, not the product frontend. Its manifest declares a development preview service, not a production service.
- Shared UI primitives and template packages enlarge the workspace but do not by themselves establish product capabilities.
- `replit.md` is stale template documentation claiming Express/PostgreSQL and a required `DATABASE_URL`. The README is closer to reality but lacks the current lineage routes, frontend test instructions, and complete Python installation requirements.

## 3. How XML is parsed today

### Parsing

`api/parser/parser.py:43–122`:

1. Parses bytes with lxml. DTD loading, DTD validation, network access, entity resolution, recovery, and huge-tree mode are disabled.
2. Requires the root's local tag name to be `POWERMART`.
3. Rejects custom entity declarations and remaining entity-reference nodes. An external `powrmart.dtd` is not fetched.
4. Walks direct `REPOSITORY > FOLDER` children. Tag and attribute **names** are matched case-insensitively and by namespace-local name.
5. Collects folder-level SOURCE and TARGET definitions, direct MAPPING elements, and referenced same-folder reusable TRANSFORMATION definitions.
6. Builds fields with name, datatype, precision, and scale; ports also carry port type and expression. Precision/scale remain strings.
7. Builds mapping instances and connectors. Source/target association uses instance definition names.
8. Copies direct `TABLEATTRIBUTE` name/value pairs into dictionaries on supported objects. This is not preservation of every XML attribute or arbitrary nested structure.

The IR carries sources, targets, transformations, instances, connectors, and repository/folder context. It is deliberately much smaller than the original XML.

### Ordering and lineage

- `order.py:6–38` rejects exact duplicate instance names, unknown connector instances, and instance-level cycles. Parallel field connectors count as one instance dependency. Isolated instances survive.
- This order is a **dependency ordering**, not a simulation of PowerCenter workflow/session execution.
- `lineage.py:21–81` creates one graph node per instance and groups field connectors into instance-to-instance edges.
- `lineage.py:83–140` traverses upstream iteratively, avoiding Python recursion limits.
- For an output port with an expression, it strips single-quoted literals and regex-matches local ports whose port type contains `INPUT`. Otherwise it follows incoming connectors.
- A constant expression such as `SYSDATE` terminates at that expression port. Multi-input expressions such as FULL_NAME can reach both source fields.
- Returned hops are a deduplicated, depth-first list, not an explicit branch DAG or a complete execution explanation. The UI already describes this distinction.

### What is not converted

**There is no XML-to-dbt conversion stage.** Expression strings are displayed, not transpiled. TABLEATTRIBUTE values such as SQL overrides and filter conditions are stored, not compiled or executed.

There is no datatype/dialect mapping, source/ref generation, model naming/materialization policy, incremental/update strategy, SQL/YAML exporter, dbt manifest, or dbt validation pipeline.

### Proposed conversion pipeline — not implemented

```text
Hardened XML ingest
  -> richer, versioned IR + completeness diagnostics
  -> reference/port/type validation
  -> transformation capability checks
  -> expression parser and semantic dependency analysis
  -> supported relational operations
  -> warehouse-specific SQL/dbt source()/ref() generation
  -> deterministic models + sources/schema YAML + diagnostics bundle
  -> dbt validation and result-equivalence tests
```

Do not translate by concatenating raw XML expressions into SQL or by treating the topological instance list as a complete compilation plan. Unknown or unsupported semantics must block a claim of a valid migration rather than silently disappear.

## 4. Transformation and XML support boundaries

**All transformation types are unsupported for dbt generation today.** “Parsed” or “coloured in the graph” does not mean semantically supported.

| Construct/type | Current inspection/lineage behavior | Missing for migration or complete semantic lineage |
| --- | --- | --- |
| Source/Target definitions | Basic fields and table attributes; instances/connectors visualized. | Owner/schema/connection metadata, richer field constraints, dialect types, dbt sources/targets and load behavior. |
| Source Qualifier | Generic ports/attributes and connector traversal. | SQL overrides, joins, source filters, DISTINCT, query semantics, and source-to-output analysis. |
| Expression | Limited expression text matching of input-port names; simple sample cases work. | Expression AST, local-variable chains, function resolution, parameters, null/date/string semantics, and target-dialect translation. |
| Filter | Connected column paths remain visible; condition is retained as an attribute. | Predicate compilation and row/control-dependency explanation. Column-value lineage alone does not explain why rows are present. |
| Joiner | Type can be coloured; generic ports/connectors retained. | Join keys/types, master/detail semantics, output lineage and row effects. |
| Lookup / Lookup Procedure | Generic capture; lookup-like names receive a colour. | Connected/unconnected lookup calls, lookup source/query, conditions, defaults, cache and multiple-match behavior. |
| Aggregator | Generic capture and colour classification. | Grouping, aggregate functions, sorted-input assumptions, and row/cardinality effects. |
| Router, Union, Sorter, Rank | Generic TRANSFORMATION/TRANSFORMFIELD/TABLEATTRIBUTE data can survive; UI falls back to Unknown where no type classification exists. | Group-specific structures, branching/recombination, ordering, distinctness, ranking and tie semantics. |
| Sequence Generator, Update Strategy, Normalizer, Transaction Control | Generic metadata only where represented by the recognized XML structures. | Stateful IDs, insert/update/delete behavior, row expansion, transaction/load semantics; some need explicit redesign rather than direct dbt equivalents. |
| SQL/Stored Procedure/Java/custom transformations | Unknown types are preserved, not dropped. | External code/runtime behavior and side effects; do not infer safe SQL equivalence. |
| Same-folder reusable transformations | Referenced definitions are included. | Cross-folder/shared-object resolution and deeper execution semantics. |
| Mapplets, shortcuts, sessions, workflows, parameter/connection overrides | No dedicated expansion or execution model. Their unrecognized XML structures are not represented by the current IR. | Explicit parsing, scope resolution, completeness reporting, and an agreed migration policy. |

Evidence: `parser.py:18–40,62–121`; `lineage.py:118–139`; frontend `lineage-utils.ts:6–31`. The colour legend is presentation logic, not a compiler capability registry.

## 5. Correctness gaps and missing error handling

### Confirmed observations

| Finding | Evidence and consequence | Recommended handling |
| --- | --- | --- |
| Structurally empty exports succeed | `<POWERMART/>` produces empty lists without a warning. The UI can show “No mappings found,” but there is no completeness status. | Decide whether source-only/empty exports are valid inspection inputs; otherwise reject, or return a structured diagnostic. |
| Missing definitions/ports can be treated as a valid graph | Ordering validates instance endpoints, not definition resolution or field existence. `lineage.py:87–95` accepts a field mentioned only in a connector. | Validate known definitions/ports; explicitly mark connector-only fallback and unresolved objects. Preserve unknown types without claiming completeness. |
| Local-variable expression lineage is incomplete | Only ports containing `INPUT` are reference candidates (`lineage.py:123–125`). A synthetic `T.B -> E.B`, where `E.B = v` and local variable `v = A`, stopped before `A` and its source. | Parse local variables/dependencies or report an unsupported-expression diagnostic. |
| Identifier handling is inconsistent | Parser definition association and graph ordering use exact identifiers; lineage dictionaries/lookups case-fold names; frontend port resolution is exact (`parser.py:90–118`, `order.py:8–19`, `lineage.py:27–49`, `lineage-utils.ts:63–80`). | Agree identifier-case rules; validate collisions and use one resolution policy. XML tag/attribute case support is a separate concern. |
| Definition type aliases are not resolved consistently | Parser accepts Source/Target Definition aliases, while lineage definition selection branches only on instance type `source` or `target` (`parser.py:90–99`, `lineage.py:35–49`). | Normalize instance kinds once and test alias exports, including unconnected declared fields. |
| Reduced metadata has no loss report | Models omit many XML attributes and nested constructs; names and field values can default to empty strings (`models.py:4–64`, `parser.py:25–39`). | Add required-field/reference validation and machine-readable warnings with mapping/instance/field context. |
| Expected and unexpected errors have different contracts | Parse/trace catch `ValueError`; framework form validation uses 422 `detail`; sample stat/open/read and upload I/O failures have no application-level handling (`main.py:27–37,65–66,89–103,149–150`). | Normalize validation and internal error responses; log sanitized diagnostics with a request ID, not raw XML. Return safe 5xx errors for unexpected failures. |
| Handwritten and runtime API contracts can drift | OpenAPI YAML drives Orval, but FastAPI returns `JSONResponse` without typed response models. Generated Zod schemas are not applied by the HTTP client; success data is cast to `T` (`custom-fetch.ts:325–370`). | Establish one contract source and contract tests. Document/validate 422 and error bodies consistently. |
| No application request deadline/cancellation policy | Fetch forwards options but adds no default timeout. Trace request IDs prevent stale UI results but do not cancel network/backend work (`lineage-view.tsx:114–142`). | Set deadlines, cancel obsolete client requests where possible, and independently bound backend execution. |
| Readiness can be misleading | Health always returns OK; frontend serving is conditional on the asset directory at import time (`main.py:19–24,153–154`). | Separate liveness from readiness, and validate required release assets/configuration before declaring the web service ready. |
| Optional theme storage can break the page | `App.tsx:75–83` reads/writes localStorage without handling storage errors. | Treat unavailable storage as a recoverable preference failure. |

Synthetic probes performed during review:

- Empty POWERMART: successful empty IR.
- Local-variable chain: hops were `T.B`, `E.B`; `E.v`, `E.A`, and `S.A` were absent.
- A connector to undeclared `T.NOT_A_COLUMN` could be traced successfully because the connector itself made the field acceptable.

These observations do not prove all real exports are affected. They establish cases that need validation or explicit unsupported status.

### Error handling already present

Do not remove or duplicate the existing extension/size checks, safe XML errors, entity rejection, duplicate/cycle/unknown-instance errors, ambiguous mapping errors, sample-not-found responses, trace retry/missing-input messages, stale-response guards, or production-safe render fallback.

## 6. Security, privacy, and availability

### Existing safeguards

- lxml XXE/network/DTD protections and explicit custom-entity rejection.
- Sample selection is through a filename allowlist; symlinks are excluded. No direct user-controlled sample path join was found.
- Upload handles are closed in a `finally` block.
- No observed execution of uploaded XML expressions, SQL, Java, or external transformation code.
- Product XML/IR values are rendered as React text, not injected as HTML.
- No intentional upload database, external LLM submission, or application-record persistence.
- `.agents/` is ignored and untracked in Git. This does not establish what is packaged into release images.

### Gaps and deployment-dependent risks

1. **High: application-level file validation is not an ingress body limit.** FastAPI parses multipart input before `read_xml_upload()` reads at most 5 MiB plus one byte. Overall body size, extra file parts, concurrency, and upload duration are not capped by this code. Verify any upstream controls and enforce bounds before parsing/spooling.
2. **High: CPU/memory/output expansion is unbounded at the product level.** XML parsing, repeated ordering, graph construction, and JSON serialization run synchronously inside async routes. Reused definitions serialized into many mappings can expand output far beyond input size. Every trace reparses the document. Bound nodes/ports/connectors/mappings, response size, queue depth, concurrency, and execution time; choose suitable worker isolation. Merely adding `async` or a client timeout does not solve this.
3. **Privacy statement is too strong.** The installed Starlette multipart parser uses a 1 MiB spooling threshold and can write larger uploads to temporary files before the handler reads them. The UI's “checked in memory, not saved” wording is therefore not guaranteed (`App.tsx:298–302`). No deliberate durable storage was found, but temporary-disk use, cleanup, logging, browser memory/caches, and retention need an explicit policy.
4. **Access model is undecided.** An unauthenticated parser may be appropriate for a controlled tool, but public proprietary-export processing needs an explicit audience/privacy decision and abuse controls. Preserve the no-auth scope unless requirements change; private deployment/network restrictions are alternatives to application accounts.
5. **Production boundary settings are unverified.** The FastAPI app does not define security-header, cache/privacy-header, trusted-host, or rate-limit middleware. Check the actual proxy before deciding which controls belong there versus in the app. The unused Express CORS/logger settings must not be counted as FastAPI protection.
6. **Browser and packaging policy need review.** Google Fonts are requested externally (`index.html:16–18`, `src/index.css:1`). Decide whether enterprise/offline requirements require self-hosting. Define a compatible CSP/framing policy. Audit release inclusion of samples, attachments, internal notes, tests and unused scaffolds; `.replitignore` currently excludes only `.local`, not an explicit product allowlist. No publicly reachable source-directory leak was demonstrated.
7. **Sanitize operational errors and keep samples intentional.** The render boundary logs complete error objects to the console (`error-boundary.tsx:77–82`). Review telemetry forwarding/redaction before handling confidential exports. Bundled samples must be approved for public visibility; uploads must never automatically become samples.

### Dependency audit at review time

Read-only `pnpm audit --json`: **0 critical, 0 high, 3 moderate, 2 low**. These are five advisories affecting two resolved packages, not five distinct affected packages.

| Package | Resolved / safe minimum reported by audit | Findings and observed path |
| --- | --- | --- |
| `fast-uri` | `3.1.8` / `>=3.1.9` | Two moderate, two low URI-normalization/confusion advisories. Orval → Scalar parser/validator → Ajv → fast-uri, in API code-generation tooling. |
| `postcss-selector-parser` | `6.0.10` / `>=6.1.5` | One moderate CPU-exhaustion advisory. `@tailwindcss/typography` dependency in frontend build tooling. |

Advisory references: [fast-uri scheme decoding](https://github.com/advisories/GHSA-w74x-v6f9-j3xg), [path equivalence](https://github.com/advisories/GHSA-xhq7-xmqj-769h), [host canonicalization](https://github.com/advisories/GHSA-wjr9-j795-jxcr), [suffix scheme injection](https://github.com/advisories/GHSA-333x-w4r6-6crp), [selector parser complexity](https://github.com/advisories/GHSA-rj75-hqrm-r3gf).

These paths were observed in development/build tooling, not the active Python upload handler. That reduces direct runtime reachability, not supply-chain/build risk. No dependency fixes were applied. Python advisories and release-image dependencies still need their own audit; zero high Node findings is not a whole-application security guarantee.

## 7. Prioritized delivery plan

Priority definitions: **P0** = release gate for the intended public/production capability; **P1** = next implementation tranche; **P2** = follow-on scale/operational improvements. Dependencies are explicit; these are recommendations, not queued or authorized tasks.

### P0 — establish a truthful, deployable, bounded inspector

| Order | Work | Acceptance criteria |
| --- | --- | --- |
| 1 | Confirm release objective, supported inputs, audience, and lineage definition. State the current lack of dbt generation clearly. | A documented inspection-vs-migration scope and support policy; UI/API do not imply semantic completeness for unsupported constructs. |
| 2 | Make Python installation and release startup reproducible. | One canonical, locked dependency path; direct `networkx` import is satisfied. The README's requirements-file install is fixed or replaced: that file currently omits NetworkX. A clean release boots the actual Python service, serves built assets, and supports sample/file parse and trace through the intended routes without relying on workspace-installed packages. |
| 3 | Bound untrusted request and processing resources. | Enforced total-body/file-count/size/duration limits before expensive processing; graph/output/concurrency/time budgets; controlled failure responses; load/adversarial tests show the service remains responsive. |
| 4 | Add import/reference validation and completeness diagnostics. | Unknown types remain preserved, but missing definitions/ports, unsupported constructs and incomplete expression lineage are explicit. Test the synthetic cases in section 5 and representative real exports. No silent “complete” result for an unsupported mapping. |

Public release needs all applicable P0 gates. A private pilot may reduce exposure but does not make lineage semantics correct.

### P1 — reliability and maintainability

1. **Unify error/API contracts.** Typed response validation, standardized error codes/context/request IDs, consistent 422 responses, safe unexpected-error handling, and generated-contract drift checks. Test invalid multipart requests, I/O failures, and malformed responses.
2. **Fix workspace/setup inconsistencies.** Remove or guard the registered DB-push setup behavior after scope approval; reconcile `replit.md`, README, Python dependency declarations, and product-specific build instructions. Decide whether to keep two Python production services and the unused Node/DB scaffolds; do not delete or rewire them blindly.
3. **Expand automated coverage and CI.** Frozen dependency installation; Python tests; frontend tests with Python dependencies available; workspace typecheck; product builds; contract consistency; published-topology smoke tests. Commit real-browser coverage for graph fitting/resizing and trace behavior: the current DOM tests mock the canvas.
4. **Address remaining audited packages narrowly.** Upgrade only affected tooling dependencies to compatible safe versions; rerun relevant tests/typecheck/build/audits. Add Python dependency scanning and dependency/license review to the release checks.
5. **Define data handling and operations.** Correct temporary-storage messaging, document cleanup/retention and sample policy, add privacy-safe latency/error/resource metrics, cancellation/deadline behavior, and readiness checks. Choose infrastructure versus app controls based on the agreed access model.

### dbt generation — an additional release gate if a migrator is the goal

This is missing product functionality, not a small hardening patch. Start after agreeing the warehouse and supported semantics:

1. Define a capability registry and richer, versioned IR, including required source/target metadata and diagnostics.
2. Implement expression parsing, variable-port resolution, and dependency analysis before attempting SQL translation.
3. Start with an explicitly limited subset: source/target wiring, supported Source Qualifier behavior, Expression functions and Filter predicates. Reject nonempty unsupported SQL overrides or other semantic features rather than approximating them silently.
4. Emit deterministic model SQL, `sources.yml`/schema YAML, dbt `source()`/`ref()` references, naming/quoting rules, documentation and a diagnostic/export manifest. Choose model granularity and materializations explicitly.
5. Add joins/lookups/aggregates and other transformations only with independent semantic fixtures. Stateful, procedural, external-code, or DML-oriented constructs may require a documented manual-redesign path.
6. Validate with `dbt parse`/`dbt compile` and approved warehouse-backed result comparisons. Cover nulls, dates/time zones, decimals, duplicate rows, join cardinality, filter behavior and incremental updates. Warehouse validation is separate from adding an application database.

**Migration acceptance:** supported exports produce repeatable, compiling output with verified results; unsupported constructs produce actionable blockers; partial output is never labelled production-equivalent.

### P2 — scale and usability

- Avoid the initial double upload and repeated full parse on every trace, subject to the agreed no-persistence policy. Any in-memory reuse needs bounded size/lifetime and isolation; a database is not required.
- Build an explicit trace dependency DAG/edge list for branch-aware explanations instead of inferring all path edges from a flat hop set.
- Benchmark larger, branched and multi-mapping exports. Add graph grouping/virtualization or declared size limits. The current minimum zoom of 0.2 can prevent fitting sufficiently large graphs, and the inspector eagerly stringifies all IR even when Raw JSON is collapsed.
- Add keyboard/screen-reader interaction tests, reduced-motion behavior, guarded theme storage, and offline/font policy checks.
- Document release, rollback and incident procedures; test graceful overload/shutdown and the exact production image, not only development preview.

## 8. Questions requiring product intent

1. **Release goal:** should the next production milestone be a hardened inspector, actual dbt generation, or a staged delivery of both?
2. **Warehouse/dbt target:** Snowflake, BigQuery, Databricks, Redshift, Postgres, or another adapter? Which dbt version and deployment style?
3. **Audience/data:** internal controlled use or a public service? Are proprietary production exports permitted, and is temporary-disk processing acceptable?
4. **PowerCenter scope:** which versions/export variants and representative transformation types are required? Are sessions, workflows, mapplets and shared-folder shortcuts in scope?
5. **Lineage meaning:** column-value provenance only, or also filter/join/lookup/aggregate row dependencies? May incomplete lineage be shown with warnings?
6. **Unsupported behavior:** should conversion fail for an entire mapping, emit a clearly blocked partial bundle, or support explicitly approved manual steps?
7. **dbt design:** models per target or another granularity? Required naming, source schema/owner rules, materializations, incremental keys, and update/delete semantics?
8. **Operational limits:** expected file/mapping/graph sizes, concurrent users, latency targets, retention requirements and offline/enterprise restrictions?

Until these are answered, the P0 inspector plan is provisional and the dbt compiler section is a proposed design, not an agreed implementation specification.
