const REQUIRED_VARIABLES = new Set(["DS_PROMETHEUS", "app_name"]);
const JOB_FILTER = 'job="$app_name"';
const VIZ_WITH_QUERIES = new Set(["stat", "timeseries", "bargauge"]);
const VALID_VARIABLE_SORT_VALUES = new Set([
  "disabled",
  "alphabeticalAsc",
  "alphabeticalDesc",
  "numericalAsc",
  "numericalDesc",
  "alphabeticalCaseInsensitiveAsc",
  "alphabeticalCaseInsensitiveDesc",
  "naturalAsc",
  "naturalDesc",
]);

export const DASHBOARD_RULE_PROFILES = {
  imageMigration: {
    expectedUid: "brandsearch-image-migration",
    expectedTitle: "Гардиум. Миграция изображений: EORA → Image",
    metricPrefix: "tm_image_",
    extraMetricNames: ["tm_image_search_backend_info"],
  },
  catalogImport: {
    expectedUid: "brandsearch-catalog-import",
    expectedTitle: "Гардиум. Импорт товарных знаков и заявок",
    metricPrefix: "tm_import_",
    extraMetricNames: [],
  },
  smartLb: {
    expectedUid: "gpustack-smart-lb",
    expectedTitle: "GPUStack. Smart Load Balancer",
    metricPrefix: "gpustack:lb_",
    extraMetricNames: [],
  },
};

const PROFILE_BY_UID = Object.fromEntries(
  Object.entries(DASHBOARD_RULE_PROFILES).map(([id, profile]) => [profile.expectedUid, { id, ...profile }]),
);

export function resolveDashboardRuleProfile(dashboard) {
  const uid = dashboard?.uid;
  if (typeof uid === "string" && uid in PROFILE_BY_UID) {
    return PROFILE_BY_UID[uid];
  }
  return null;
}

function requireMapping(data, path, errors) {
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    errors.push(`${path}: ожидается object, получено ${data === null ? "null" : Array.isArray(data) ? "array" : typeof data}`);
    return null;
  }
  return data;
}

function requireList(data, path, errors) {
  if (!Array.isArray(data)) {
    errors.push(`${path}: ожидается array, получено ${data === null ? "null" : typeof data}`);
    return null;
  }
  return data;
}

function iterPanelQueries(dashboard) {
  const elements = dashboard.elements;
  if (typeof elements !== "object" || elements === null) {
    return [];
  }

  const queries = [];
  for (const [elementName, element] of Object.entries(elements)) {
    if (typeof element !== "object" || element === null || element.kind !== "Panel") {
      continue;
    }
    const spec = element.spec;
    if (typeof spec !== "object" || spec === null) {
      continue;
    }
    const data = spec.data;
    if (typeof data !== "object" || data === null) {
      continue;
    }
    const dataSpec = data.spec;
    if (typeof dataSpec !== "object" || dataSpec === null) {
      continue;
    }
    const panelQueries = dataSpec.queries;
    if (!Array.isArray(panelQueries)) {
      continue;
    }
    for (const [index, panelQuery] of panelQueries.entries()) {
      if (typeof panelQuery !== "object" || panelQuery === null) {
        continue;
      }
      const querySpec = panelQuery.spec;
      if (typeof querySpec !== "object" || querySpec === null) {
        continue;
      }
      const query = querySpec.query;
      if (typeof query !== "object" || query === null) {
        continue;
      }
      const promSpec = query.spec;
      if (typeof promSpec === "object" && promSpec !== null) {
        queries.push([`elements.${elementName}.queries[${index}]`, promSpec]);
      }
    }
  }
  return queries;
}

function validatePanelElement(elementName, element, errors) {
  const path = `elements.${elementName}`;
  if (element.kind !== "Panel") {
    errors.push(`${path}.kind: ожидается 'Panel'`);
    return null;
  }

  const spec = requireMapping(element.spec, `${path}.spec`, errors);
  if (spec === null) {
    return null;
  }

  const panelId = spec.id;
  if (typeof panelId !== "number" || !Number.isInteger(panelId)) {
    errors.push(`${path}.spec.id: ожидается целое число`);
  }

  const vizConfig = requireMapping(spec.vizConfig, `${path}.spec.vizConfig`, errors);
  if (vizConfig === null) {
    return typeof panelId === "number" ? panelId : null;
  }

  const vizSpec = requireMapping(vizConfig.spec, `${path}.spec.vizConfig.spec`, errors);
  if (vizSpec !== null) {
    const fieldConfig = requireMapping(vizSpec.fieldConfig, `${path}.spec.vizConfig.spec.fieldConfig`, errors);
    if (fieldConfig !== null) {
      if (!Array.isArray(fieldConfig.overrides)) {
        errors.push(`${path}.spec.vizConfig.spec.fieldConfig.overrides: ожидается array`);
      }
      if (!("defaults" in fieldConfig)) {
        errors.push(`${path}.spec.vizConfig.spec.fieldConfig: отсутствует поле 'defaults'`);
      }
    }
  }

  const vizGroup = vizConfig.group;
  if (VIZ_WITH_QUERIES.has(vizGroup)) {
    if (!spec.title) {
      errors.push(`${path}.spec.title: ожидается непустой заголовок`);
    }
    if (!spec.description) {
      errors.push(`${path}.spec.description: ожидается описание панели`);
    }
  }

  if (vizGroup === "text" && vizSpec !== null) {
    const options = requireMapping(vizSpec.options, `${path}.spec.vizConfig.spec.options`, errors);
    if (options !== null && !options.content) {
      errors.push(`${path}.spec.vizConfig.spec.options.content: текстовая панель без содержимого`);
    }
  }

  return typeof panelId === "number" ? panelId : null;
}

function validateMetadata(dashboard, profile, errors) {
  if ("panels" in dashboard) {
    errors.push("panels: обнаружен legacy-формат; ожидается Grafana schema v2 (elements/layout)");
  }
  if ("schemaVersion" in dashboard) {
    errors.push("schemaVersion: legacy-поле не используется в Grafana schema v2");
  }

  if (!dashboard.title) {
    errors.push("title: ожидается непустой заголовок");
  } else if (dashboard.title !== profile.expectedTitle) {
    errors.push(`title: ожидается ${JSON.stringify(profile.expectedTitle)}, получено ${JSON.stringify(dashboard.title)}`);
  }

  if (!dashboard.description) {
    errors.push("description: ожидается описание дашборда");
  }

  if (dashboard.uid !== profile.expectedUid) {
    errors.push(`uid: ожидается ${JSON.stringify(profile.expectedUid)}, получено ${JSON.stringify(dashboard.uid)}`);
  }

  if (typeof dashboard.version !== "number" || dashboard.version < 1) {
    errors.push("version: ожидается целое число >= 1");
  }

  const annotations = requireList(dashboard.annotations, "annotations", errors);
  if (annotations !== null && annotations.length === 0) {
    errors.push("annotations: ожидается хотя бы одна запись");
  }
}

function validateElements(dashboard, errors) {
  const elements = requireMapping(dashboard.elements, "elements", errors);
  const elementNames = new Set();
  const panelIds = [];
  if (elements === null) {
    return elementNames;
  }

  if (Object.keys(elements).length === 0) {
    errors.push("elements: ожидается хотя бы одна панель");
  }

  for (const [elementName, element] of Object.entries(elements)) {
    elementNames.add(elementName);
    if (typeof element !== "object" || element === null) {
      errors.push(`elements.${elementName}: ожидается object`);
      continue;
    }
    const panelId = validatePanelElement(elementName, element, errors);
    if (panelId !== null) {
      panelIds.push(panelId);
    }
  }

  if (panelIds.length !== new Set(panelIds).size) {
    errors.push("elements: id панелей должны быть уникальными");
  }

  return elementNames;
}

function validateLayout(dashboard, elementNames, errors) {
  const layout = requireMapping(dashboard.layout, "layout", errors);
  if (layout === null) {
    return;
  }
  if (layout.kind !== "GridLayout") {
    errors.push("layout.kind: ожидается 'GridLayout'");
  }

  const layoutSpec = requireMapping(layout.spec, "layout.spec", errors);
  if (layoutSpec === null) {
    return;
  }

  const items = requireList(layoutSpec.items, "layout.spec.items", errors);
  if (items === null) {
    return;
  }

  for (const [index, item] of items.entries()) {
    if (typeof item !== "object" || item === null || item.kind !== "GridLayoutItem") {
      errors.push(`layout.spec.items[${index}]: ожидается GridLayoutItem`);
      continue;
    }
    const itemSpec = item.spec;
    if (typeof itemSpec !== "object" || itemSpec === null) {
      continue;
    }
    const element = itemSpec.element;
    if (typeof element !== "object" || element === null) {
      continue;
    }
    const refName = element.name;
    if (typeof refName === "string" && !elementNames.has(refName)) {
      errors.push(`layout.spec.items[${index}]: ссылка на неизвестный element ${JSON.stringify(refName)}`);
    }
  }
}

function validateVariables(dashboard, errors) {
  const variables = requireList(dashboard.variables, "variables", errors);
  if (variables === null) {
    return;
  }

  const names = new Set();
  for (const [index, variable] of variables.entries()) {
    if (typeof variable !== "object" || variable === null) {
      continue;
    }
    const spec = variable.spec;
    if (typeof spec !== "object" || spec === null) {
      continue;
    }
    if (typeof spec.name === "string") {
      names.add(spec.name);
    }
    const current = requireMapping(spec.current, `variables[${index}].spec.current`, errors);
    if (current === null) {
      continue;
    }
    for (const field of ["text", "value"]) {
      if (!(field in current)) {
        errors.push(`variables[${index}].spec.current: отсутствует поле ${JSON.stringify(field)}`);
      }
    }
    if (variable.kind === "QueryVariable" && typeof spec.sort === "string" && !VALID_VARIABLE_SORT_VALUES.has(spec.sort)) {
      errors.push(
        `variables[${index}].spec.sort: недопустимое значение ${JSON.stringify(spec.sort)}; ожидается одно из ${[...VALID_VARIABLE_SORT_VALUES].join(", ")}`,
      );
    }
  }

  const missingAll = [...REQUIRED_VARIABLES].filter((name) => !names.has(name)).sort();
  if (missingAll.length > 0) {
    errors.push(`variables: отсутствуют переменные ${JSON.stringify(missingAll)}`);
  }
}

function validateQueries(dashboard, profile, errors) {
  const extraMetricNames = profile.extraMetricNames ?? [];
  for (const [queryPath, promSpec] of iterPanelQueries(dashboard)) {
    const expr = promSpec.expr;
    if (typeof expr !== "string" || !expr.trim()) {
      errors.push(`${queryPath}.expr: ожидается непустая PromQL-строка`);
      continue;
    }
    if (!expr.includes(JOB_FILTER)) {
      errors.push(`${queryPath}.expr: отсутствует фильтр ${JSON.stringify(JOB_FILTER)}`);
    }
    const hasMetricPrefix = expr.includes(profile.metricPrefix);
    const hasExtraMetric = extraMetricNames.some((name) => expr.includes(name));
    if (!hasMetricPrefix && !hasExtraMetric) {
      errors.push(
        `${queryPath}.expr: ожидается метрика ${profile.metricPrefix}*${extraMetricNames.length > 0 ? ` или ${extraMetricNames.join(" / ")}` : ""}`,
      );
    }
  }
}

export function validateDashboardRules(dashboard, profile = resolveDashboardRuleProfile(dashboard)) {
  const errors = [];
  if (profile === null) {
    errors.push(`uid: неизвестный профиль правил для ${JSON.stringify(dashboard?.uid ?? null)}`);
    return errors;
  }
  validateMetadata(dashboard, profile, errors);
  const elementNames = validateElements(dashboard, errors);
  validateLayout(dashboard, elementNames, errors);
  validateVariables(dashboard, errors);
  validateQueries(dashboard, profile, errors);
  return errors;
}
