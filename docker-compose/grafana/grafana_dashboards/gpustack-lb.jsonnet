// Grafana dashboard v2: Smart Load Balancer (SmartLoadBalancingStrategy)
// Генерация: make dashboards
// Скрипты: docker-compose/grafana/grafana_dashboards/scripts/ (npm run dashboards)

local ds = { name: '${DS_PROMETHEUS}' };

local jobFilter = 'job="$app_name"';
local modelFilter = 'job="$app_name",model_name="$model_name"';

local metric(name) = name + '{' + jobFilter + '}';
local metricByModel(name) = name + '{' + modelFilter + '}';
local sumMetric(name) = 'sum(' + metric(name) + ')';
local sumMetricByModel(name) = 'sum(' + metricByModel(name) + ')';
local sumByReason(name) = 'sum by (reason) (' + metricByModel(name) + ')';
local sumByReqClass(name) = 'sum by (request_class) (' + metricByModel(name) + ')';
local sumByInstance(name) = 'sum by (instance_id) (' + metricByModel(name) + ')';
local rateByReason(name, interval='5m') = 'sum by (reason) (rate(' + metricByModel(name) + '[' + interval + ']))';
local rateByReqClass(name, interval='5m') = 'sum by (request_class) (rate(' + metricByModel(name) + '[' + interval + ']))';

local defaultAppName = 'gpustack';
local defaultDatasourceText = 'Prometheus';
local defaultDatasourceUid = 'prometheus';

local variableCurrent(text, value) = {
  text: text,
  value: value,
};

local dashboardDescription = |||
  Дашборд визуализирует работу **SmartLoadBalancingStrategy** — балансировщика GPUStack.

  Источник данных — Prometheus-метрики `gpustack:lb_*` на `/metrics` HTTP-прокси.

  **Ключевые метрики:**
  1. Распределение prompt-размеров — профиль KV-нагрузки
  2. Эффективность affinity — как часто сессии прилипают к реплике
  3. Балансировка нагрузки — равномерность распределения по score
  4. Affinity streak — не монополизирует ли сессия реплику
  5. Классификация запросов — доля short/medium/heavy
  6. Задержка выбора — накладные расходы балансировщика
|||
;

local dashboardTextContent = |||
  # Smart Load Balancer

  Дашборд визуализирует работу **SmartLoadBalancingStrategy** — балансировщика GPUStack.

  Источник данных — Prometheus-метрики `gpustack:lb_*` на `/metrics` HTTP-прокси.

  **Ключевые метрики:**
  1. Распределение prompt-размеров — профиль KV-нагрузки
  2. Эффективность affinity — как часто сессии прилипают к реплике
  3. Балансировка нагрузки — равномерность распределения по score
  4. Affinity streak — не монополизирует ли сессия реплику
  5. Классификация запросов — доля short/medium/heavy
  6. Задержка выбора — накладные расходы балансировщика
|||
;

local promDataQuery(expr, legend='', instant=false) = {
  datasource: ds,
  group: 'prometheus',
  kind: 'DataQuery',
  spec: {
    editorMode: 'code',
    exemplar: true,
    expr: expr,
    instant: instant,
    interval: '',
    legendFormat: legend,
    range: !instant,
  },
  version: 'v0',
};

local panelQuery(expr, legend='', instant=false, refId='A') = {
  kind: 'PanelQuery',
  spec: {
    hidden: false,
    query: promDataQuery(expr, legend, instant),
    refId: refId,
  },
};

local queryGroup(queries) = {
  kind: 'QueryGroup',
  spec: {
    queries: queries,
    queryOptions: {},
    transformations: [],
  },
};

local statOptions = {
  colorMode: 'value',
  graphMode: 'area',
  justifyMode: 'auto',
  orientation: 'auto',
  percentChangeColorMode: 'standard',
  reduceOptions: {
    calcs: ['lastNotNull'],
    fields: '',
    values: false,
  },
  showPercentChange: false,
  textMode: 'auto',
  wideLayout: true,
};

local statViz(fieldConfig) = {
  group: 'stat',
  kind: 'VizConfig',
  spec: {
    fieldConfig: fieldConfig,
    options: statOptions,
  },
  version: '13.1.0',
};

local timeseriesCustom = {
  axisBorderShow: false,
  axisCenteredZero: false,
  axisColorMode: 'text',
  axisLabel: '',
  axisPlacement: 'auto',
  barAlignment: 0,
  barWidthFactor: 0.6,
  drawStyle: 'line',
  fillOpacity: 10,
  gradientMode: 'none',
  hideFrom: {
    legend: false,
    tooltip: false,
    viz: false,
  },
  insertNulls: false,
  lineInterpolation: 'linear',
  lineWidth: 1,
  pointSize: 5,
  scaleDistribution: { type: 'linear' },
  showPoints: 'auto',
  showValues: false,
  spanNulls: false,
  stacking: { group: 'A', mode: 'none' },
  thresholdsStyle: { mode: 'off' },
};

local timeseriesStackedCustom = {
  axisBorderShow: false,
  axisCenteredZero: false,
  axisColorMode: 'text',
  axisLabel: '',
  axisPlacement: 'auto',
  barAlignment: 0,
  barWidthFactor: 0.6,
  drawStyle: 'line',
  fillOpacity: 20,
  gradientMode: 'none',
  hideFrom: {
    legend: false,
    tooltip: false,
    viz: false,
  },
  insertNulls: false,
  lineInterpolation: 'linear',
  lineWidth: 1,
  pointSize: 5,
  scaleDistribution: { type: 'linear' },
  showPoints: 'auto',
  showValues: false,
  spanNulls: false,
  stacking: { group: 'A', mode: 'normal' },
  thresholdsStyle: { mode: 'off' },
};

local timeseriesViz(unit='short', stacked=false) = {
  group: 'timeseries',
  kind: 'VizConfig',
  spec: {
    fieldConfig: {
      defaults: {
        color: { mode: 'palette-classic' },
        custom: if stacked then timeseriesStackedCustom else timeseriesCustom,
        thresholds: {
          mode: 'absolute',
          steps: [
            { color: 'green', value: 0 },
            { color: 'red', value: 80 },
          ],
        },
        unit: unit,
      },
      overrides: [],
    },
    options: {
      legend: {
        calcs: [],
        displayMode: 'list',
        enableFacetedFilter: false,
        overflow: 'ellipsis',
        placement: 'bottom',
        showLegend: true,
      },
      tooltip: {
        hideZeros: false,
        mode: if stacked then 'multi' else 'single',
        sort: 'none',
      },
    },
  },
  version: '13.1.0',
};

local bargaugeViz(unit='short') = {
  group: 'bargauge',
  kind: 'VizConfig',
  spec: {
    fieldConfig: {
      defaults: {
        color: { mode: 'continuous-GrYlRd' },
        thresholds: {
          mode: 'absolute',
          steps: [
            { color: 'green', value: 0 },
            { color: 'red', value: 80 },
          ],
        },
        unit: unit,
      },
      overrides: [],
    },
    options: {
      displayMode: 'gradient',
      minVizHeight: 10,
      minVizWidth: 0,
      orientation: 'horizontal',
      reduceOptions: {
        calcs: ['lastNotNull'],
        fields: '',
        values: false,
      },
      showUnfilled: true,
      valueMode: 'color',
    },
  },
  version: '13.1.0',
};

local mkPanel(id, title, description, data, vizConfig) = {
  ['panel-' + std.toString(id)]: {
    kind: 'Panel',
    spec: {
      data: data,
      description: description,
      id: id,
      links: [],
      title: title,
      vizConfig: vizConfig,
    },
  },
};

local statPanel(id, title, description, expr, fieldConfig) =
  mkPanel(
    id,
    title,
    description,
    queryGroup([panelQuery(expr, '', true)]),
    statViz(fieldConfig),
  );

local layoutItem(id, x, y, w, h) = {
  kind: 'GridLayoutItem',
  spec: {
    element: {
      kind: 'ElementReference',
      name: 'panel-' + std.toString(id),
    },
    height: h,
    width: w,
    x: x,
    y: y,
  },
};

local defaultField = {
  thresholds: {
    mode: 'absolute',
    steps: [{ color: 'green', value: 0 }],
  },
  unit: 'short',
};

local emptyFieldConfig = {
  defaults: {},
  overrides: [],
};

// ---------------------------------------------------------------------------
// Text panel
// ---------------------------------------------------------------------------

local textPanel = mkPanel(
  100,
  '',
  'Описание Smart Load Balancer дашборда.',
  queryGroup([]),
  {
    group: 'text',
    kind: 'VizConfig',
    spec: {
      fieldConfig: emptyFieldConfig,
      options: {
        code: {
          language: 'plaintext',
          showLineNumbers: false,
          showMiniMap: false,
        },
        content: dashboardTextContent,
        mode: 'markdown',
      },
    },
    version: '13.1.0',
  },
);

// ---------------------------------------------------------------------------
// Row 1: Ключевые показатели
// ---------------------------------------------------------------------------

// 1. Распределение prompt-размеров (histogram percentiles)
local promptTokensPanel = mkPanel(
  1,
  'Prompt Tokens (percentiles)',
  'Распределение размера prompt в токенах по моделям. Показывает реальный профиль KV-нагрузки.',
  queryGroup([
    panelQuery('histogram_quantile(0.5, sum by (le, model_name) (rate(gpustack:lb_request_prompt_tokens_bucket{job="$app_name"}[5m])))', 'p50', false, 'A'),
    panelQuery('histogram_quantile(0.9, sum by (le, model_name) (rate(gpustack:lb_request_prompt_tokens_bucket{job="$app_name"}[5m])))', 'p90', false, 'B'),
    panelQuery('histogram_quantile(0.95, sum by (le, model_name) (rate(gpustack:lb_request_prompt_tokens_bucket{job="$app_name"}[5m])))', 'p95', false, 'C'),
    panelQuery('histogram_quantile(0.99, sum by (le, model_name) (rate(gpustack:lb_request_prompt_tokens_bucket{job="$app_name"}[5m])))', 'p99', false, 'D'),
  ]),
  timeseriesViz('short'),
);

// 2. Классификация запросов (short/medium/heavy)
local requestClassPanel = mkPanel(
  2,
  'Request Classification',
  'Доля short/medium/heavy запросов во времени. Определяет scoring weights балансировщика.',
  queryGroup([
    panelQuery('sum by (request_class, model_name) (rate(gpustack:lb_requests_total{job="$app_name"}[5m]))', '{{request_class}}', false, 'A'),
  ]),
  timeseriesViz('reqps', true),
);

// 3. Задержка выбора (selection latency percentiles)
local selectionLatencyPanel = mkPanel(
  3,
  'Selection Latency',
  'Накладные расходы балансировщика. Время select_instance() в секундах.',
  queryGroup([
    panelQuery('histogram_quantile(0.5, sum by (le, model_name) (rate(gpustack:lb_selection_latency_seconds_bucket{job="$app_name"}[5m])))', 'p50', false, 'A'),
    panelQuery('histogram_quantile(0.9, sum by (le, model_name) (rate(gpustack:lb_selection_latency_seconds_bucket{job="$app_name"}[5m])))', 'p90', false, 'B'),
    panelQuery('histogram_quantile(0.95, sum by (le, model_name) (rate(gpustack:lb_selection_latency_seconds_bucket{job="$app_name"}[5m])))', 'p95', false, 'C'),
    panelQuery('histogram_quantile(0.99, sum by (le, model_name) (rate(gpustack:lb_selection_latency_seconds_bucket{job="$app_name"}[5m])))', 'p99', false, 'D'),
  ]),
  timeseriesViz('s'),
);

// ---------------------------------------------------------------------------
// Row 2: Affinity и балансировка
// ---------------------------------------------------------------------------

// 4. Эффективность affinity (affinity_soft vs pot_score)
local affinityEfficiencyPanel = mkPanel(
  4,
  'Affinity Efficiency',
  'Соотношение affinity_soft vs pot_score в lb_selections_total. Показывает, насколько часто affinity срабатывает.',
  queryGroup([
    panelQuery('sum by (reason, model_name) (rate(gpustack:lb_selections_total{job="$app_name"}[5m]))', '{{reason}}', false, 'A'),
  ]),
  timeseriesViz('reqps', true),
);

// 5. Affinity streak по инстансам
local affinityStreakPanel = mkPanel(
  5,
  'Affinity Streak',
  'Текущий streak последовательных affinity-хитов по инстансам. Не монополизирует ли одна сессия реплику.',
  queryGroup([
    panelQuery('gpustack:lb_instance_affinity_streak{job="$app_name"}', '{{instance_id}}', true, 'A'),
  ]),
  bargaugeViz('short'),
);

// 6. Streak resets
local streakResetsPanel = mkPanel(
  6,
  'Streak Resets',
  'Частота принудительного сброса affinity streak (cap exceeded). Показывает, как часто срабатывает лимит.',
  queryGroup([
    panelQuery('sum by (model_name) (rate(gpustack:lb_affinity_streak_resets_total{job="$app_name"}[5m]))', '{{model_name}}', false, 'A'),
  ]),
  timeseriesViz('ops'),
);

// ---------------------------------------------------------------------------
// Row 3: Нагрузка на инстансы
// ---------------------------------------------------------------------------

// 7. Балансировка нагрузки (instance score)
local instanceScorePanel = mkPanel(
  7,
  'Instance Score',
  'Текущий score по инстансам. Насколько равномерно распределяется нагрузка (ниже score — лучше).',
  queryGroup([
    panelQuery('gpustack:lb_instance_score{job="$app_name"}', '{{instance_id}}', true, 'A'),
  ]),
  bargaugeViz('short'),
);

// 8. EWMA KV cache
local ewmaKvPanel = mkPanel(
  8,
  'EWMA KV Cache',
  'Сглаженное использование KV cache (Peak EWMA) по инстансам. Показывает реальную KV-нагрузку.',
  queryGroup([
    panelQuery('gpustack:lb_instance_ewma_kv{job="$app_name"}', '{{instance_id}}', true, 'A'),
  ]),
  bargaugeViz('percentunit'),
);

// 9. WLC вес
local wlcWeightPanel = mkPanel(
  9,
  'WLC Weight',
  'Weighted Least Connections вес по инстансам. Сумма весов активных соединений (prompt + max_tokens).',
  queryGroup([
    panelQuery('gpustack:lb_instance_wlc_weight{job="$app_name"}', '{{instance_id}}', true, 'A'),
  ]),
  bargaugeViz('short'),
);

// ---------------------------------------------------------------------------
// Row 4: Динамика
// ---------------------------------------------------------------------------

// 10. Score во времени
local scoreOverTimePanel = mkPanel(
  10,
  'Score Over Time',
  'Динамика score по инстансам. Показывает, как меняется нагрузка на каждую реплику.',
  queryGroup([
    panelQuery('gpustack:lb_instance_score{job="$app_name"}', '{{instance_id}}', false, 'A'),
  ]),
  timeseriesViz('short'),
);

// 11. Pool size
local poolSizePanel = mkPanel(
  11,
  'Pool Size',
  'Число здоровых реплик в пуле. Должно быть стабильным; падения указывают на проблемы с инстансами.',
  queryGroup([
    panelQuery('gpustack:lb_pool_size{job="$app_name"}', '{{model_name}}', false, 'A'),
  ]),
  timeseriesViz('short'),
);

// 12. Slow start вес
local slowStartPanel = mkPanel(
  12,
  'Slow Start Weight',
  'Slow start вес по инстансам. 0 = активен, 1 = полностью idle. Показывает warm-up после простоя.',
  queryGroup([
    panelQuery('gpustack:lb_instance_slow_start_weight{job="$app_name"}', '{{instance_id}}', false, 'A'),
  ]),
  timeseriesViz('short'),
);

// ---------------------------------------------------------------------------
// Row 5: Суммарные показатели по репликам
// ---------------------------------------------------------------------------

// 13. Суммарное количество запросов по репликам
local totalRequestsPanel = mkPanel(
  13,
  'Total Requests per Replica',
  'Суммарное количество запросов, распределённых по каждой реплике (instance_id).',
  queryGroup([
    panelQuery('sum by (instance_id, model_name) (rate(gpustack:lb_selections_total{job="$app_name"}[5m]))', '{{instance_id}}', false, 'A'),
  ]),
  timeseriesViz('reqps', true),
);

// 14. Суммарная пропускная способность генерации токенов по репликам
local totalGenerationTokensPanel = mkPanel(
  14,
  'Total Generation Tokens per Replica',
  'Суммарная пропускная способность по генерации токенов (max_tokens) по каждой реплике.',
  queryGroup([
    panelQuery('sum by (instance_id, model_name) (rate(gpustack:lb_selections_total{job="$app_name"}[5m])) * on (model_name) group_left() (avg by (model_name) (rate(gpustack:lb_request_max_tokens_sum{job="$app_name"}[5m])))', '{{instance_id}}', false, 'A'),
  ]),
  timeseriesViz('short', true),
);

// 15. Суммарная input context способность по репликам
local totalInputContextPanel = mkPanel(
  15,
  'Total Input Context per Replica',
  'Суммарная input context способность (prompt_tokens) по каждой реплике.',
  queryGroup([
    panelQuery('sum by (instance_id, model_name) (rate(gpustack:lb_selections_total{job="$app_name"}[5m])) * on (model_name) group_left() (avg by (model_name) (rate(gpustack:lb_request_prompt_tokens_sum{job="$app_name"}[5m])))', '{{instance_id}}', false, 'A'),
  ]),
  timeseriesViz('short', true),
);

// ---------------------------------------------------------------------------
// Panels aggregation
// ---------------------------------------------------------------------------

local panels =
  textPanel
  + promptTokensPanel
  + requestClassPanel
  + selectionLatencyPanel
  + affinityEfficiencyPanel
  + affinityStreakPanel
  + streakResetsPanel
  + instanceScorePanel
  + ewmaKvPanel
  + wlcWeightPanel
  + scoreOverTimePanel
  + poolSizePanel
  + slowStartPanel
  + totalRequestsPanel
  + totalGenerationTokensPanel
  + totalInputContextPanel;

{
  annotations: [
    {
      kind: 'AnnotationQuery',
      spec: {
        builtIn: true,
        enable: true,
        hide: true,
        iconColor: 'rgba(0, 211, 255, 1)',
        name: 'Annotations & Alerts',
        query: {
          datasource: { name: 'grafana' },
          group: 'grafana',
          kind: 'DataQuery',
          spec: {
            limit: 100,
            matchAny: false,
            tags: [],
            type: 'dashboard',
          },
          version: 'v0',
        },
      },
    },
  ],
  cursorSync: 'Crosshair',
  description: dashboardDescription,
  editable: true,
  elements: panels,
  layout: {
    kind: 'GridLayout',
    spec: {
      items: [
        // Row 0: description
        layoutItem(100, 0, 0, 24, 15),
        // Row 1: Key metrics
        layoutItem(1, 0, 15, 8, 8),
        layoutItem(2, 8, 15, 8, 8),
        layoutItem(3, 16, 15, 8, 8),
        // Row 2: Affinity
        layoutItem(4, 0, 23, 8, 8),
        layoutItem(5, 8, 23, 8, 8),
        layoutItem(6, 16, 23, 8, 8),
        // Row 3: Instance load
        layoutItem(7, 0, 31, 8, 8),
        layoutItem(8, 8, 31, 8, 8),
        layoutItem(9, 16, 31, 8, 8),
        // Row 4: Dynamics
        layoutItem(10, 0, 39, 12, 8),
        layoutItem(11, 12, 39, 6, 8),
        layoutItem(12, 18, 39, 6, 8),
        // Row 5: Aggregate metrics per replica
        layoutItem(13, 0, 47, 8, 8),
        layoutItem(14, 8, 47, 8, 8),
        layoutItem(15, 16, 47, 8, 8),
      ],
    },
  },
  links: [],
  liveNow: false,
  preload: false,
  tags: ['gpustack', 'load-balancer', 'smart-lb'],
  timeSettings: {
    autoRefresh: '30s',
    autoRefreshIntervals: ['5s', '10s', '30s', '1m', '5m', '15m', '30m', '1h', '2h', '1d'],
    fiscalYearStartMonth: 0,
    from: 'now-3h',
    hideTimepicker: false,
    timezone: 'browser',
    to: 'now',
  },
  title: 'GPUStack. Smart Load Balancer',
  uid: 'gpustack-smart-lb',
  variables: [
    {
      kind: 'QueryVariable',
      spec: {
        allowCustomValue: true,
        current: variableCurrent(defaultAppName, defaultAppName),
        definition: 'label_values(gpustack:lb_requests_total,job)',
        hide: 'dontHide',
        includeAll: false,
        label: 'Application Name',
        multi: false,
        name: 'app_name',
        options: [],
        query: {
          datasource: ds,
          group: 'prometheus',
          kind: 'DataQuery',
          spec: {
            qryType: 1,
            query: 'label_values(gpustack:lb_requests_total,job)',
            refId: 'PrometheusVariableQueryEditor-VariableQuery',
          },
          version: 'v0',
        },
        refresh: 'onDashboardLoad',
        regex: '',
        regexApplyTo: 'value',
        skipUrlSync: false,
        sort: 'disabled',
      },
    },
    {
      kind: 'QueryVariable',
      spec: {
        allowCustomValue: true,
        current: variableCurrent('all', ''),
        definition: 'label_values(gpustack:lb_requests_total, model_name)',
        hide: 'dontHide',
        includeAll: false,
        label: 'Model Name',
        multi: false,
        name: 'model_name',
        options: [],
        query: {
          datasource: ds,
          group: 'prometheus',
          kind: 'DataQuery',
          spec: {
            qryType: 1,
            query: 'label_values(gpustack:lb_requests_total, model_name)',
            refId: 'PrometheusVariableQueryEditor-VariableQuery',
          },
          version: 'v0',
        },
        refresh: 'onDashboardLoad',
        regex: '',
        regexApplyTo: 'value',
        skipUrlSync: false,
        sort: 'disabled',
      },
    },
    {
      kind: 'DatasourceVariable',
      spec: {
        allowCustomValue: true,
        current: variableCurrent(defaultDatasourceText, defaultDatasourceUid),
        hide: 'dontHide',
        includeAll: false,
        label: 'Datasource',
        multi: false,
        name: 'DS_PROMETHEUS',
        options: [],
        pluginId: 'prometheus',
        refresh: 'onDashboardLoad',
        regex: '',
        skipUrlSync: false,
      },
    },
  ],
  version: std.parseInt(std.extVar('dashboard_version')),
}
