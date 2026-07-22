/**
 * Валидация dashboard JSON по OpenAPI-схеме Grafana v2 (@grafana/openapi).
 *
 * Сырые OpenAPI-спеки описывают opaque map (options, DataQuery.spec) как object-only;
 * Grafana UI применяет те же ослабления — см. PR grafana#118107.
 */

import { readFileSync } from "node:fs";

import Ajv from "ajv";
import addFormats from "ajv-formats";

import { OPENAPI_SCHEMA_PATH } from "./paths.mjs";

function loadOpenApiSpec() {
  const raw = readFileSync(OPENAPI_SCHEMA_PATH, "utf8");
  return JSON.parse(raw);
}

function relaxOpaqueMapSchemas(spec) {
  const schemas = spec.components?.schemas;
  if (!schemas) {
    throw new Error("OpenAPI spec: отсутствует components.schemas");
  }

  if (schemas.DashboardVizConfigSpec?.properties?.options) {
    schemas.DashboardVizConfigSpec.properties.options = {
      type: "object",
      additionalProperties: true,
    };
  }

  if (schemas.DashboardDataQueryKind?.properties?.spec) {
    schemas.DashboardDataQueryKind.properties.spec = {
      type: "object",
      additionalProperties: true,
    };
  }
}

function formatAjvErrors(errors) {
  return errors.map((error) => {
    const path = error.instancePath || "/";
    return `${path}: ${error.message}`;
  });
}

export function validateDashboardSchema(dashboard, spec = loadOpenApiSpec()) {
  relaxOpaqueMapSchemas(spec);

  const ajv = new Ajv({ allErrors: true, strict: false, validateSchema: false });
  addFormats(ajv);
  ajv.addSchema(spec);

  const validate = ajv.getSchema("#/components/schemas/DashboardSpec");
  if (!validate) {
    throw new Error("Не найдена схема #/components/schemas/DashboardSpec в @grafana/openapi");
  }

  if (!validate(dashboard)) {
    return formatAjvErrors(validate.errors ?? []);
  }
  return [];
}

export function ensureOpenApiInstalled() {
  try {
    readFileSync(OPENAPI_SCHEMA_PATH, "utf8");
  } catch {
    throw new Error("OpenAPI-схема Grafana не установлена; выполни: npm ci");
  }
}
