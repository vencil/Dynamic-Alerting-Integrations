/**
 * Test-only guard for fixtures that stand in for a tenant-api response (#2833).
 *
 * The portal's tests used to hand-write tenant-api bodies, and a hand-written
 * body can only agree with the component that reads it: the pending-PR link
 * read `html_url`, the fixture said `html_url`, the test passed — and the API
 * sends `web_url`. The fixture has to agree with the API instead, so every
 * such fixture goes through `apiFixture(<definition>, body)`, which validates
 * it against the generated OpenAPI spec (components/tenant-api/docs/swagger.json,
 * built by `make api-docs` from the Go annotations) and throws when it does not
 * fit. ajv is a devDependency: none of this reaches the bundle.
 *
 * What "fits" means here is stricter than the spec on purpose: every object
 * schema that does not say otherwise gets `additionalProperties: false`, so a
 * field the API does not send (the `html_url` shape) is rejected rather than
 * carried along. Object schemas that declare `additionalProperties` (maps such
 * as MeResponse.permissions) keep their own rule. A field the spec does not
 * list as `required` may be left out (most are; ErrorResponse requires `error`
 * and `code`), so a fixture carries only what its test is about.
 *
 * Swagger 2.0 vs ajv's draft-07: the only non-JSON-Schema keyword that changes
 * validation is `x-nullable`, translated below; `format` is not asserted (as in
 * tenant-schema.ts); other `x-*` keys are ignored under strict:false.
 */
import Ajv from 'ajv';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const SWAGGER = JSON.parse(
  readFileSync(resolve(here, '../../../../components/tenant-api/docs/swagger.json'), 'utf8'),
);
export const API_DEFINITIONS: Record<string, any> = SWAGGER.definitions;

const SCHEMA_ID = 'tenant-api-swagger';

/** Deep-copies one schema node, closing open objects and translating x-nullable. */
function strictify(node: any): any {
  if (Array.isArray(node)) return node.map(strictify);
  if (node === null || typeof node !== 'object') return node;
  const out: any = {};
  for (const [k, v] of Object.entries(node)) {
    if (k === 'properties') {
      out.properties = Object.fromEntries(
        Object.entries(v as Record<string, any>).map(([p, s]) => [p, strictify(s)]),
      );
    } else if (['items', 'additionalProperties', 'not', 'allOf', 'anyOf', 'oneOf'].includes(k)) {
      out[k] = strictify(v);
    } else {
      out[k] = v;
    }
  }
  if ((out.type === 'object' || out.properties) && !('additionalProperties' in out)) {
    out.additionalProperties = false;
  }
  if (out['x-nullable'] === true) {
    if (typeof out.type === 'string') return { ...out, type: [out.type, 'null'] };
    return { anyOf: [out, { type: 'null' }] };
  }
  return out;
}

const ajv = new Ajv({ strict: false, allErrors: true, validateFormats: false });
ajv.addSchema({
  $id: SCHEMA_ID,
  definitions: Object.fromEntries(
    Object.entries(API_DEFINITIONS).map(([name, s]) => [name, strictify(s)]),
  ),
});

/**
 * Resolves a definition name. The full name always works; a short name (the
 * part after the last `.`, e.g. `PRInfo`) works only when exactly one
 * definition in the spec ends that way — an unknown or ambiguous short name
 * throws, never silently picks one.
 */
export function resolveDefinition(name: string, defs: Record<string, unknown> = API_DEFINITIONS): string {
  if (name in defs) return name;
  const hits = Object.keys(defs).filter(k => k.slice(k.lastIndexOf('.') + 1) === name);
  if (hits.length === 1) return hits[0];
  throw new Error(
    hits.length === 0
      ? `apiFixture: no definition named "${name}" in tenant-api swagger.json`
      : `apiFixture: "${name}" is ambiguous in tenant-api swagger.json: ${hits.join(', ')}`,
  );
}

const compiled = new Map<string, ReturnType<typeof ajv.compile>>();

function validatorFor(name: string) {
  const isArray = name.endsWith('[]');
  const full = resolveDefinition(isArray ? name.slice(0, -2) : name);
  const key = isArray ? `${full}[]` : full;
  if (!compiled.has(key)) {
    const ref = { $ref: `${SCHEMA_ID}#/definitions/${full}` };
    compiled.set(key, ajv.compile(isArray ? { type: 'array', items: ref } : ref));
  }
  return { key, validate: compiled.get(key)! };
}

export type ApiFixtureError = { instancePath: string; keyword: string; message?: string; params: any };

/** Returns the reasons `data` is not a `name` body; [] when it is. */
export function apiFixtureErrors(name: string, data: unknown): ApiFixtureError[] {
  const { validate } = validatorFor(name);
  return validate(data) ? [] : (validate.errors as ApiFixtureError[]).map(
    ({ instancePath, keyword, message, params }) => ({ instancePath, keyword, message, params }),
  );
}

/**
 * Returns `data` unchanged when it is a valid `name` body (a definition name,
 * full or short; append `[]` for an endpoint that returns an array of it), and
 * throws otherwise — the test fails at the fixture, naming the offending field.
 */
export function apiFixture<T>(name: string, data: T): T {
  const errors = apiFixtureErrors(name, data);
  if (errors.length > 0) {
    const { key } = validatorFor(name);
    const lines = errors.map(e => {
      const extra = e.params?.additionalProperty ? ` "${e.params.additionalProperty}"` : '';
      return `  ${e.instancePath || '/'} ${e.keyword}${extra}: ${e.message}`;
    });
    throw new Error(`apiFixture: fixture is not a valid ${key} (tenant-api swagger.json):\n${lines.join('\n')}`);
  }
  return data;
}
