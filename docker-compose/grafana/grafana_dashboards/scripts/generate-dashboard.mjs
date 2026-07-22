import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { Jsonnet } from "@hanazuki/node-jsonnet";

import { DASHBOARD_TARGETS } from "./paths.mjs";
import { validateDashboardFile } from "./validate-dashboard.mjs";

const JSON_INDENT = 2;

export function formatDashboardJson(dashboard) {
  return `${JSON.stringify(dashboard, null, JSON_INDENT)}\n`;
}

export function nextDashboardVersion(path) {
  try {
    const data = JSON.parse(readFileSync(path, "utf8"));
    const current = data.version ?? 0;
    if (typeof current !== "number" || current < 1) {
      return 1;
    }
    return current + 1;
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
      return 1;
    }
    throw error;
  }
}

export async function renderDashboard({ jsonnetPath, version }) {
  return new Jsonnet()
    .extString("dashboard_version", String(version))
    .evaluateFile(jsonnetPath);
}

export async function generateDashboardTarget(target) {
  const version = nextDashboardVersion(target.json);
  const raw = await renderDashboard({ jsonnetPath: target.jsonnet, version });
  const output = formatDashboardJson(JSON.parse(raw));
  writeFileSync(target.json, output, "utf8");
  await validateDashboardFile(target.json);
  console.log(`Wrote and validated ${target.json} (version=${version}, ${output.length - 1} bytes)`);
}

export async function main() {
  for (const target of DASHBOARD_TARGETS) {
    await generateDashboardTarget(target);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  });
}
