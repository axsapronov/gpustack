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

// Вычисляемые метрики для новых панелей
local affinityHitRate = 'sum(rate(gpustack:lb_selections_total{job="$app_name",reason="affinity_soft"}[5m])) / sum(rate(gpustack:lb_selections_total{job="$app_name"}[5m])) * 100';
local loadImbalanceRatio = 'max(gpustack:lb_instance_score{job="$app_name"}) / min(gpustack:lb_instance_score{job="$app_name"})';
local kvEfficiencyRatio = 'avg(gpustack:lb_instance_ewma_kv{job="$app_name"}) / avg(gpustack:lb_request_kv_cache_usage{job="$app_name"})';

local defaultAppName = 'gpustack';
local defaultDatasourceText = 'Prometheus';
local defaultDatasourceUid = 'prometheus';

local variableCurrent(text, value) = {
  text: text,
  value: value,
};

local dashboardDescription = |||
  Дашборд визуализирует работу **SmartLoadBalancingStrategy** — балансировщика GPUStack.
|||;

local dashboardTextContent = |||
  # Smart Load Balancer

  Дашборд визуализирует работу **SmartLoadBalancingStrategy** — балансировщика GPUStack.
|||;

// Text panels для каждой секции с пояснениями что смотреть
local section1TextContent = |||
  ### Нагрузка (Load Intensity)

  Эта секция показывает общую интенсивность трафика, проходящего через балансер.

  **Что смотреть для анализа:**
  - **Request Rate** — мгновенная скорость запросов. Резкие скачки указывают на всплески нагрузки.
  - **Request Classification** — распределение по типу (short/medium/heavy). Сдвиг к heavy запросам означает рост вычислительной нагрузки.
  - **Token Throughput** — общая пропускная способность в токенах/мин. Коррелирует с загрузкой GPU.
  - **Prompt Tokens (percentiles)** — распределение размеров контекста. Высокие p95/p99 указывают на тяжёлые запросы, влияющие на KV-cache.
|||;

local section2TextContent = |||
  ### Эффективность балансировщика (Balancer Effectiveness)

  Секция показывает, насколько эффективно работает стратегия балансировки — affinity, streak-лимиты, и KV-cache сглаживание.

  **Что смотреть для анализа:**
  - **Affinity Efficiency** — соотношение причин селекции. Доминирование `affinity_soft` означает стабильные сессии.
  - **Affinity Hit Rate (%)** — процент affinity-хитов. >60% — хорошо, <30% — сессии нестабильны.
  - **Affinity Streak** — текущая серия affinity-хитов по репликам. Слишком высокие значения = монополизация.
  - **Streak Resets** — частота сбросов streak. Высокое значение = агрессивный cap или нестабильные сессии.
  - **KV Cache Usage Over Time** — динамика raw и EWMA KV-кэша по инстансам. Сравнивайте raw с EWMA для оценки пиковости нагрузки.
  - **KV Cache Usage (raw)** — мгновенное использование KV-кэша. Показывает пиковую нагрузку на VRAM.
  - **EWMA KV Cache** — сглаженное использование KV. Сравнивайте с raw для оценки пиковости.
  - **KV Efficiency Ratio** — отношение EWMA/raw. <1 означает эффективное сглаживание пиков.
|||;

local section3TextContent = |||
  ### Утилизация ресурсов (Resource Utilization)

  Секция показывает, насколько равномерно распределяется нагрузка между репликами.

  **Что смотреть для анализа:**
  - **Requests per Replica** — распределение запросов. Идеально — равномерные линии по репликам.
  - **Instance Score** — текущий score по репликам. Низкий score = свободная реплика.
  - **Score Over Time** — динамика score. Расхождения между линиями указывают на дисбаланс.
  - **Load Imbalance Ratio** — коэффициент дисбаланса (max/min score). 1 = идеально, >2 = перекос.
  - **WLC Weight** — вес активных соединений. Показывает вычислительную нагрузку на каждую реплику.
  - **Pool Size** — число здоровых реплик. Падения указывают на проблемы с инстансами.
|||;

local section4TextContent = |||
  ### Отладка (Debug)

  Секция для глубокой диагностики работы балансировщика — latency, slow start, и токены по репликам.

  **Что смотреть для анализа:**
  - **Selection Latency** — время работы `select_instance()`. Высокие p99 = проблемы с алгоритмом или пулом.
  - **Slow Start Weight** — вес slow-start по репликам. 1 = idle, 0 = активен. Должен быстро опускаться после простоя.
  - **Generation Tokens per Replica** — генерация токенов по репликам. Показывает реальную вычислительную нагрузку.
  - **Input Context per Replica** — input context по репликам. Коррелирует с KV-cache давлением.
|||;

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

local statViz(defaults) = {
  group: 'stat',
  kind: 'VizConfig',
  spec: {
    fieldConfig: {
      defaults: defaults,
      overrides: [],
    },
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

local textSectionPanel(id, title, content) = mkPanel(
  id,
  title,
  '',
  queryGroup([]),
  {
    group: 'text',
    kind: 'VizConfig',
    spec: {
      fieldConfig: {
        defaults: {},
        overrides: [],
      },
      options: {
        code: {
          language: 'plaintext',
          showLineNumbers: false,
          showMiniMap: false,
        },
        content: content,
        mode: 'markdown',
      },
    },
    version: '13.1.0',
  },
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
// Text panel (главное описание)
// ---------------------------------------------------------------------------

local mainTextPanel = mkPanel(
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

// Text panels для секций
local section1TextPanel = textSectionPanel(101, '', section1TextContent);
local section2TextPanel = textSectionPanel(102, '', section2TextContent);
local section3TextPanel = textSectionPanel(103, '', section3TextContent);
local section4TextPanel = textSectionPanel(104, '', section4TextContent);

// ---------------------------------------------------------------------------
// Section 1: Нагрузка (Load Intensity)
// ---------------------------------------------------------------------------

// 1. Общая скорость запросов (stat)
local requestRatePanel = statPanel(
  1,
  'Request Rate',
  'Общая скорость запросов через балансер (запросов в минуту).',
  'sum(rate(gpustack:lb_requests_total{job="$app_name"}[5m])) * 60',
  {
    unit: 'rpm',
    thresholds: {
      mode: 'absolute',
      steps: [
        { color: 'green', value: 0 },
        { color: '#EAB839', value: 50 },
        { color: 'red', value: 100 },
      ],
    },
  },
);

// 2. Классификация запросов (short/medium/heavy)
local requestClassPanel = mkPanel(
  2,
  'Request Classification',
  'Доля short/medium/heavy запросов во времени. Определяет scoring weights балансировщика.',
  queryGroup([
    panelQuery('sum by (request_class, model_name) (rate(gpustack:lb_requests_total{job="$app_name"}[5m])) * 60', '{{request_class}}', false, 'A'),
  ]),
  timeseriesViz('rpm', true),
);

// 3. Токенный throughput
local tokenThroughputPanel = mkPanel(
  3,
  'Token Throughput',
  'Среднее количество токенов в минуту (prompt + max_tokens). Показывает общую вычислительную нагрузку.',
  queryGroup([
    panelQuery('sum(rate(gpustack:lb_request_total_tokens_sum{job="$app_name"}[5m])) * 60', 'tokens/min', false, 'A'),
  ]),
  timeseriesViz('short'),
);

// 4. Распределение prompt-размеров (histogram percentiles)
local promptTokensPanel = mkPanel(
  4,
  'Prompt Tokens (percentiles)',
  'Распределение размера prompt в токенах. Показывает реальный профиль KV-нагрузки.',
  queryGroup([
    panelQuery('histogram_quantile(0.5, sum by (le, model_name) (rate(gpustack:lb_request_prompt_tokens_bucket{job="$app_name"}[5m])))', 'p50', false, 'A'),
    panelQuery('histogram_quantile(0.9, sum by (le, model_name) (rate(gpustack:lb_request_prompt_tokens_bucket{job="$app_name"}[5m])))', 'p90', false, 'B'),
    panelQuery('histogram_quantile(0.95, sum by (le, model_name) (rate(gpustack:lb_request_prompt_tokens_bucket{job="$app_name"}[5m])))', 'p95', false, 'C'),
    panelQuery('histogram_quantile(0.99, sum by (le, model_name) (rate(gpustack:lb_request_prompt_tokens_bucket{job="$app_name"}[5m])))', 'p99', false, 'D'),
  ]),
  timeseriesViz('short'),
);

// ---------------------------------------------------------------------------
// Section 2: Эффективность балансировщика (Balancer Effectiveness)
// ---------------------------------------------------------------------------

// 5. Эффективность affinity (affinity_soft vs pot_score и другие причины)
local affinityEfficiencyPanel = mkPanel(
  5,
  'Affinity Efficiency',
  'Соотношение affinity_soft vs pot_score в lb_selections_total. Показывает, насколько часто affinity срабатывает.',
  queryGroup([
    panelQuery('sum by (reason, model_name) (rate(gpustack:lb_selections_total{job="$app_name"}[5m])) * 60', '{{reason}}', false, 'A'),
  ]),
  timeseriesViz('rpm', true),
);

// 6. Affinity Hit Rate (%) — новая панель
local affinityHitRatePanel = statPanel(
  6,
  'Affinity Hit Rate (%)',
  'Процент запросов, обслуженных через affinity (affinity_soft / все селекции). Высокое значение означает, что сессии стабильно прилипают к репликам.',
  'sum(rate(gpustack:lb_selections_total{job="$app_name",reason="affinity_soft"}[5m])) / sum(rate(gpustack:lb_selections_total{job="$app_name"}[5m])) * 100',
  {
    unit: 'percent',
    min: 0,
    max: 100,
    thresholds: {
      mode: 'absolute',
      steps: [
        { color: 'red', value: 0 },
        { color: '#EAB839', value: 30 },
        { color: 'green', value: 60 },
      ],
    },
  },
);

// 7. Affinity streak по инстансам
local affinityStreakPanel = mkPanel(
  7,
  'Affinity Streak',
  'Текущий streak последовательных affinity-хитов по инстансам. Не монополизирует ли одна сессия реплику.',
  queryGroup([
    panelQuery('gpustack:lb_instance_affinity_streak{job="$app_name"}', '{{instance_id}}', true, 'A'),
  ]),
  bargaugeViz('short'),
);

// 8. Streak resets
local streakResetsPanel = mkPanel(
  8,
  'Streak Resets',
  'Частота принудительного сброса affinity streak (cap exceeded). Показывает, как часто срабатывает лимит.',
  queryGroup([
    panelQuery('sum by (model_name) (rate(gpustack:lb_affinity_streak_resets_total{job="$app_name"}[5m])) * 60', '{{model_name}}', false, 'A'),
  ]),
  timeseriesViz('rpm'),
);

// 9. KV Cache Usage (raw) — новая панель
local rawKvCachePanel = mkPanel(
  9,
  'KV Cache Usage (raw)',
  'Текущее использование KV cache (raw, без сглаживания) по инстансам. Показывает мгновенную нагрузку на VRAM.',
  queryGroup([
    panelQuery('gpustack:lb_request_kv_cache_usage{job="$app_name"}', '{{instance_id}}', true, 'A'),
  ]),
  bargaugeViz('percentunit'),
);

// 10. EWMA KV cache
local ewmaKvPanel = mkPanel(
  10,
  'EWMA KV Cache',
  'Сглаженное использование KV cache (Peak EWMA) по инстансам. Показывает реальную KV-нагрузку с учётом истории.',
  queryGroup([
    panelQuery('gpustack:lb_instance_ewma_kv{job="$app_name"}', '{{instance_id}}', true, 'A'),
  ]),
  bargaugeViz('percentunit'),
);

// 11. KV Efficiency Ratio — новая панель
local kvEfficiencyPanel = statPanel(
  11,
  'KV Efficiency Ratio',
  'Отношение среднего EWMA KV к среднему raw KV. Показывает, насколько эффективно EWMA сглаживает пиковые значения. Значение < 1 означает, что EWMA снижает пиковую нагрузку.',
  'avg(gpustack:lb_instance_ewma_kv{job="$app_name"}) / avg(gpustack:lb_request_kv_cache_usage{job="$app_name"})',
  {
    unit: 'short',
    thresholds: {
      mode: 'absolute',
      steps: [
        { color: '#EAB839', value: 0 },
        { color: 'green', value: 0.5 },
        { color: 'red', value: 1.1 },
      ],
    },
  },
);

// 22. KV Cache Usage Over Time — динамика raw и EWMA по инстансам
local kvCacheOverTimePanel = mkPanel(
  22,
  'KV Cache Usage Over Time',
  'Динамика KV cache usage по инстансам во времени. Raw (сплошные линии) показывает мгновенные пики, EWMA (пунктирные) — сглаженную нагрузку. Корреляция пиков raw с падениями Affinity Hit Rate указывает на KV-индуцированные сбои affinity.',
  queryGroup([
    panelQuery('gpustack:lb_request_kv_cache_usage{job="$app_name"}', '{{instance_id}} (raw)', false, 'A'),
    panelQuery('gpustack:lb_instance_ewma_kv{job="$app_name"}', '{{instance_id}} (ewma)', false, 'B'),
  ]),
  timeseriesViz('percentunit'),
);

// ---------------------------------------------------------------------------
// Section 3: Утилизация ресурсов (Resource Utilization)
// ---------------------------------------------------------------------------

// 12. Запросы на реплику
local totalRequestsPanel = mkPanel(
  12,
  'Requests per Replica',
  'Распределение запросов по репликам. Показывает, насколько равномерно балансирует нагрузку.',
  queryGroup([
    panelQuery('sum by (instance_id, model_name) (rate(gpustack:lb_selections_total{job="$app_name"}[5m])) * 60', '{{instance_id}}', false, 'A'),
  ]),
  timeseriesViz('rpm', true),
);

// 13. Instance Score (bargauge)
local instanceScorePanel = mkPanel(
  13,
  'Instance Score',
  'Текущий score по инстансам. Насколько равномерно распределяется нагрузка (ниже score — лучше).',
  queryGroup([
    panelQuery('gpustack:lb_instance_score{job="$app_name"}', '{{instance_id}}', true, 'A'),
  ]),
  bargaugeViz('short'),
);

// 14. Score во времени
local scoreOverTimePanel = mkPanel(
  14,
  'Score Over Time',
  'Динамика score по инстансам. Показывает, как меняется нагрузка на каждую реплику.',
  queryGroup([
    panelQuery('gpustack:lb_instance_score{job="$app_name"}', '{{instance_id}}', false, 'A'),
  ]),
  timeseriesViz('short'),
);

// 15. Load Imbalance Ratio — новая панель
local loadImbalancePanel = statPanel(
  15,
  'Load Imbalance Ratio',
  'Коэффициент дисбаланса нагрузки: max(score) / min(score). Значение 1 означает идеальный баланс. Чем больше, тем сильнее перекос.',
  'max(gpustack:lb_instance_score{job="$app_name"}) / min(gpustack:lb_instance_score{job="$app_name"})',
  {
    unit: 'short',
    thresholds: {
      mode: 'absolute',
      steps: [
        { color: 'green', value: 0 },
        { color: 'green', value: 1.5 },
        { color: '#EAB839', value: 2 },
        { color: 'red', value: 5 },
      ],
    },
  },
);

// 16. WLC вес
local wlcWeightPanel = mkPanel(
  16,
  'WLC Weight',
  'Weighted Least Connections вес по инстансам. Сумма весов активных соединений (prompt + max_tokens).',
  queryGroup([
    panelQuery('gpustack:lb_instance_wlc_weight{job="$app_name"}', '{{instance_id}}', true, 'A'),
  ]),
  bargaugeViz('short'),
);

// 17. Pool size
local poolSizePanel = mkPanel(
  17,
  'Pool Size',
  'Число здоровых реплик в пуле. Должно быть стабильным; падения указывают на проблемы с инстансами.',
  queryGroup([
    panelQuery('gpustack:lb_pool_size{job="$app_name"}', '{{model_name}}', false, 'A'),
  ]),
  timeseriesViz('short'),
);

// ---------------------------------------------------------------------------
// Section 4: Отладка (Debug)
// ---------------------------------------------------------------------------

// 18. Selection Latency
local selectionLatencyPanel = mkPanel(
  18,
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

// 19. Slow Start Weight
local slowStartPanel = mkPanel(
  19,
  'Slow Start Weight',
  'Slow start вес по инстансам. 0 = активен, 1 = полностью idle. Показывает warm-up после простоя.',
  queryGroup([
    panelQuery('gpustack:lb_instance_slow_start_weight{job="$app_name"}', '{{instance_id}}', false, 'A'),
  ]),
  timeseriesViz('short'),
);

// 20. Generation Tokens per Replica
local totalGenerationTokensPanel = mkPanel(
  20,
  'Generation Tokens per Replica',
  'Суммарная пропускная способность по генерации токенов (max_tokens) по каждой реплике в минуту.',
  queryGroup([
    panelQuery('sum by (instance_id, model_name) (rate(gpustack:lb_selections_total{job="$app_name"}[5m])) * 60 * on (model_name) group_left() (avg by (model_name) (rate(gpustack:lb_request_max_tokens_sum{job="$app_name"}[5m])))', '{{instance_id}}', false, 'A'),
  ]),
  timeseriesViz('short', true),
);

// 21. Input Context per Replica
local totalInputContextPanel = mkPanel(
  21,
  'Input Context per Replica',
  'Суммарная input context способность (prompt_tokens) по каждой реплике в минуту.',
  queryGroup([
    panelQuery('sum by (instance_id, model_name) (rate(gpustack:lb_selections_total{job="$app_name"}[5m])) * 60 * on (model_name) group_left() (avg by (model_name) (rate(gpustack:lb_request_prompt_tokens_sum{job="$app_name"}[5m])))', '{{instance_id}}', false, 'A'),
  ]),
  timeseriesViz('short', true),
);

// ---------------------------------------------------------------------------
// Panels aggregation
// ---------------------------------------------------------------------------

local panels =
  mainTextPanel
  // Section text panels
  + section1TextPanel
  + section2TextPanel
  + section3TextPanel
  + section4TextPanel
  // Section 1: Load Intensity
  + requestRatePanel
  + requestClassPanel
  + tokenThroughputPanel
  + promptTokensPanel
  // Section 2: Balancer Effectiveness
  + affinityEfficiencyPanel
  + affinityHitRatePanel
  + affinityStreakPanel
  + streakResetsPanel
  + rawKvCachePanel
  + ewmaKvPanel
  + kvEfficiencyPanel
  + kvCacheOverTimePanel
  // Section 3: Resource Utilization
  + totalRequestsPanel
  + instanceScorePanel
  + scoreOverTimePanel
  + loadImbalancePanel
  + wlcWeightPanel
  + poolSizePanel
  // Section 4: Debug
  + selectionLatencyPanel
  + slowStartPanel
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
      // Унифицированная раскладка:
      // Каждая секция имеет text-block (высота 4), затем ряды панелей.
      // Внутри секций:
      //   - timeseries панели занимают верхние позиции
      //   - stat/bargauge панели занимают нижние позиции
      //   - одинаковая высота для одинаковых типов панелей
      items: [
        // Row 0: main description (y=0, h=6)
        layoutItem(100, 0, 0, 24, 6),

        // === Section 1: Load Intensity ===
        // Row 1: text block (y=6, h=10)
        layoutItem(101, 0, 6, 24, 10),
        // Row 2: stat + timeseries (y=16, h=8)
        layoutItem(1, 0, 16, 6, 8),   // Request Rate (stat)
        layoutItem(2, 6, 16, 6, 8),   // Request Classification (stacked timeseries)
        layoutItem(3, 12, 16, 6, 8),  // Token Throughput (timeseries)
        layoutItem(4, 18, 16, 6, 8),  // Prompt Tokens percentiles (timeseries)

        // === Section 2: Balancer Effectiveness ===
        // Row 3: text block (y=24, h=10)
        layoutItem(102, 0, 24, 24, 10),
        // Row 4: timeseries + stat (y=34, h=8)
        layoutItem(5, 0, 34, 8, 8),   // Affinity Efficiency (stacked timeseries)
        layoutItem(6, 8, 34, 4, 8),   // Affinity Hit Rate (stat)
        layoutItem(8, 12, 34, 6, 8),  // Streak Resets (timeseries)
        layoutItem(7, 18, 34, 6, 8),  // Affinity Streak (bargauge)
        // Row 5: bargauge + stat (y=42, h=8)
        layoutItem(9, 0, 42, 8, 8),   // KV Cache Usage raw (bargauge)
        layoutItem(10, 8, 42, 8, 8),  // EWMA KV Cache (bargauge)
        layoutItem(11, 16, 42, 4, 8), // KV Efficiency Ratio (stat)
        layoutItem(22, 20, 42, 4, 8), // KV Cache Usage Over Time (timeseries)

        // === Section 3: Resource Utilization ===
        // Row 6: text block (y=50, h=10)
        layoutItem(103, 0, 50, 24, 10),
        // Row 7: timeseries (y=60, h=8)
        layoutItem(12, 0, 60, 8, 8),  // Requests per Replica (stacked timeseries)
        layoutItem(14, 8, 60, 8, 8),  // Score Over Time (timeseries)
        layoutItem(17, 16, 60, 4, 8), // Pool Size (timeseries)
        // Row 8: bargauge + stat (y=68, h=8)
        layoutItem(13, 0, 68, 6, 8),  // Instance Score (bargauge)
        layoutItem(16, 6, 68, 6, 8),  // WLC Weight (bargauge)
        layoutItem(15, 12, 68, 4, 8), // Load Imbalance Ratio (stat)

        // === Section 4: Debug ===
        // Row 9: text block (y=76, h=10)
        layoutItem(104, 0, 76, 24, 10),
        // Row 10: timeseries (y=86, h=8)
        layoutItem(18, 0, 86, 8, 8),  // Selection Latency (timeseries)
        layoutItem(19, 8, 86, 8, 8),  // Slow Start Weight (timeseries)
        layoutItem(20, 16, 86, 4, 8), // Generation Tokens per Replica (stacked timeseries)
        layoutItem(21, 20, 86, 4, 8), // Input Context per Replica (stacked timeseries)
      ],
    },
  },
  links: [],
  liveNow: false,
  preload: false,
  tags: ['gpustack', 'load-balancer', 'smart-lb'],
  timeSettings: {
    // Округление до минут: autoRefresh=1m, to=now-1m
    autoRefresh: '1m',
    autoRefreshIntervals: ['1m', '5m', '15m', '30m', '1h', '2h', '1d'],
    fiscalYearStartMonth: 0,
    from: 'now-3h',
    hideTimepicker: false,
    timezone: 'browser',
    to: 'now-1m',
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
