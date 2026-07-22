import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { DASHBOARD_TARGETS } from "./paths.mjs";
import { validateDashboardRules } from "./validate-dashboard-rules.mjs";
import { ensureOpenApiInstalled, validateDashboardSchema } from "./validate-dashboard-schema.mjs";

export function loadDashboard(path) {
  let data;
  try {
    data = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new DashboardValidationError([`${path}: невалидный JSON: ${error.message}`]);
  }
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    throw new DashboardValidationError([`${path}: корень должен быть JSON object`]);
  }
  return data;
}

export class DashboardValidationError extends Error {
  constructor(errors) {
    super(errors.join("\n"));
    this.name = "DashboardValidationError";
    this.errors = errors;
  }
}

export async function validateDashboardFile(path) {
  const dashboard = loadDashboard(path);
  const errors = [...validateDashboardRules(dashboard)];

  ensureOpenApiInstalled();
  errors.push(...validateDashboardSchema(dashboard));

  if (errors.length > 0) {
    throw new DashboardValidationError(errors);
  }
}

async function main() {
  const dashboardPaths = process.argv[2]
    ? [resolve(process.argv[2])]
    : DASHBOARD_TARGETS.map((target) => target.json);

  let hasErrors = false;
  for (const dashboardPath of dashboardPaths) {
    try {
      await validateDashboardFile(dashboardPath);
      console.log(`OK: ${dashboardPath}`);
    } catch (error) {
      hasErrors = true;
      if (error instanceof DashboardValidationError) {
        console.error(`Validation failed for ${dashboardPath}:`);
        for (const message of error.errors) {
          console.error(`  - ${message}`);
        }
      } else {
        throw error;
      }
    }
  }
  if (hasErrors) {
    process.exit(1);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  });
}
