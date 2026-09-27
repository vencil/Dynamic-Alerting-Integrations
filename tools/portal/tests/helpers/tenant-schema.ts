/**
 * Test-only validator for docs/schemas/tenant-config.schema.json (#2033).
 *
 * The portal teaches tenant-file shapes (playground templates, gallery
 * templates, schema-explorer inserts, tenant-manager fragments); the tests
 * validate what they emit against the live schema instead of a hand-written
 * copy. ajv is a devDependency: it never reaches the bundle.
 *
 * The schema has no cross-file $ref (every $ref is #/definitions/...), so
 * compiling this one file is complete. strict:false because the schema
 * carries non-vocabulary keys (`version` at the root). Formats are not
 * asserted: in draft-07 `format` is an annotation, and the repo's Python
 * schema lints (check_confd_schema.py) do not assert it either.
 */
import Ajv from 'ajv';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
export const TENANT_SCHEMA = JSON.parse(
  readFileSync(resolve(here, '../../../../docs/schemas/tenant-config.schema.json'), 'utf8'),
);

const ajv = new Ajv({ strict: false, allErrors: true, validateFormats: false });
const validate = ajv.compile(TENANT_SCHEMA);

export type SchemaError = { instancePath: string; keyword: string; message?: string; params: any };

/**
 * `_`-prefixed tenant keys that tenantConfig does not declare. The schema
 * alone lets them through: tenantConfig.additionalProperties accepts any
 * string as a threshold, which is how `_domain_policy: finance` passed while
 * the exporter reads nothing from it (#2033).
 */
export function undeclaredReservedKeys(tenantBody: Record<string, unknown> | null | undefined): string[] {
  const declared = TENANT_SCHEMA.definitions.tenantConfig.properties;
  return Object.keys(tenantBody || {}).filter(k => k.startsWith('_') && !(k in declared));
}

/** Validates a whole `tenants:` document; returns [] when it is accepted. */
export function validateTenantDoc(doc: unknown): SchemaError[] {
  return validate(doc) ? [] : (validate.errors as SchemaError[]).map(
    ({ instancePath, keyword, message, params }) => ({ instancePath, keyword, message, params }),
  );
}
