import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { formatDashboardJson, nextDashboardVersion, renderDashboard } from "./generate-dashboard.mjs";
import { DASHBOARD_TARGETS } from "./paths.mjs";
import { DashboardValidationError, loadDashboard, validateDashboardFile } from "./validate-dashboard.mjs";
import { validateDashboardRules } from "./validate-dashboard-rules.mjs";

for (const target of DASHBOARD_TARGETS) {
  describe(`grafana dashboard ${target.id}`, () => {
    it("committed JSON passes validation", async () => {
      await validateDashboardFile(target.json);
    });

    it("JSON matches jsonnet output for the same version", async () => {
      const committed = loadDashboard(target.json);
      const generated = JSON.parse(
        await renderDashboard({ jsonnetPath: target.jsonnet, version: committed.version }),
      );
      assert.deepEqual(generated, committed);
    });

    it("uses Grafana schema v2", () => {
      const dashboard = loadDashboard(target.json);
      assert.ok("elements" in dashboard);
      assert.ok("layout" in dashboard);
      assert.ok("variables" in dashboard);
      assert.ok(!("panels" in dashboard));
    });

    it("rejects missing job filter in PromQL", () => {
      const dashboard = loadDashboard(target.json);
      const panelKey = Object.keys(dashboard.elements).find((key) => {
        const panel = dashboard.elements[key];
        return panel?.kind === "Panel" && panel?.spec?.data?.spec?.queries?.length > 0;
      });
      assert.ok(panelKey, "expected at least one panel with queries");
      dashboard.elements[panelKey].spec.data.spec.queries[0].spec.query.spec.expr = "sum(metric_without_job)";
      const errors = validateDashboardRules(dashboard);
      assert.ok(errors.some((error) => error.includes("отсутствует фильтр") && error.includes("app_name")));
    });

    it("rejects duplicate panel ids", () => {
      const dashboard = loadDashboard(target.json);
      const panelKeys = Object.keys(dashboard.elements).filter(
        (key) => dashboard.elements[key]?.kind === "Panel" && key !== "panel-100" && key !== "panel-200",
      );
      assert.ok(panelKeys.length >= 2, "expected at least two panels");
      dashboard.elements[panelKeys[1]].spec.id = dashboard.elements[panelKeys[0]].spec.id;
      const errors = validateDashboardRules(dashboard);
      assert.ok(errors.some((error) => error.includes("уникальными")));
    });
  });
}

describe("grafana dashboard helpers", () => {
  it("nextDashboardVersion starts at one for missing file", () => {
    const dir = mkdtempSync(join(tmpdir(), "dashboard-version-"));
    try {
      assert.equal(nextDashboardVersion(join(dir, "missing.json")), 1);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("nextDashboardVersion increments existing file", () => {
    const dir = mkdtempSync(join(tmpdir(), "dashboard-version-"));
    const path = join(dir, "dashboard.json");
    try {
      writeFileSync(path, JSON.stringify({ version: 5 }), "utf8");
      assert.equal(nextDashboardVersion(path), 6);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("formatDashboardJson uses two-space indent", () => {
    const formatted = formatDashboardJson({ version: 1, title: "x" });
    assert.match(formatted, /^{\n {2}"version": 1/);
  });
});

describe("grafana dashboard validation errors", () => {
  it("wraps rule failures in DashboardValidationError", async () => {
    const dashboard = loadDashboard(DASHBOARD_TARGETS[0].json);
    dashboard.variables[0].spec.current = {};
    const dir = mkdtempSync(join(tmpdir(), "dashboard-invalid-"));
    const path = join(dir, "dashboard.json");
    try {
      writeFileSync(path, JSON.stringify(dashboard), "utf8");
      await assert.rejects(() => validateDashboardFile(path), DashboardValidationError);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
