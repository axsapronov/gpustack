import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptsDir = dirname(fileURLToPath(import.meta.url));

export const MONITORING_DIR = resolve(scriptsDir, "..");
export const REPO_ROOT = resolve(MONITORING_DIR, "../../..");
export const SMART_LB_JSONNET = resolve(MONITORING_DIR, "gpustack-lb.jsonnet");
export const SMART_LB_JSON = resolve(MONITORING_DIR, "gpustack-lb.json");
export const OPENAPI_SCHEMA_PATH = resolve(
  REPO_ROOT,
  "node_modules/@grafana/openapi/dist/apis/dashboard.grafana.app-v2.json",
);

export const DASHBOARD_TARGETS = [
  {
    id: "smartLb",
    jsonnet: SMART_LB_JSONNET,
    json: SMART_LB_JSON,
  },
];
