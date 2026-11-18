(function () {
  const { createApp, nextTick } = Vue;
  const message = ElementPlus.ElMessage;
  const messageBox = ElementPlus.ElMessageBox;

  createApp({
    data() {
      return {
        pageTitle: window.RELAY_PAGE_TITLE || "API 中转站",
        activeTab: "overview",
        session: { authenticated: false, username: "", csrf_token: "" },
        loginForm: { username: "admin", password: "" },
        loginError: "",
        loading: {
          session: false,
          refresh: false,
          keys: false,
          health: false,
          settings: false,
          discoverModels: false,
          upstreamDialog: false,
        },
        settings: {
          title: "API 中转站",
          route_mode: "ordered_failover",
          auto_failover_enabled: true,
          model_mappings: {},
          scheduled_probe_all_enabled: false,
          probe_interval_seconds: 300,
          request_timeout_ms: 120000,
        },
        modelMappingRows: [],
        upstreams: [],
        proxyKeys: [],
        healthItems: [],
        requests: { items: [], total: 0 },
        requestFilters: {
          page: 1,
          page_size: 20,
          proxy_key_id: "",
          upstream_id: "",
          status_group: "",
        },
        statsDays: 7,
        stats: {
          summary: {
            total_requests: 0,
            success_requests: 0,
            error_requests: 0,
            total_tokens: 0,
            avg_latency_ms: 0,
            avg_ttfb_ms: 0,
          },
          timeseries: [],
          status_breakdown: [],
          top_models: [],
          latency_by_upstream: [],
        },
        newKey: { name: "", note: "" },
        newlyCreatedKey: "",
        charts: {},
        healthProbeJob: null,
        probeModelSelections: {},
        probeEndpointSelections: {},
        draggingUpstreamId: null,
        upstreamDialog: {
          visible: false,
          mode: "create",
          form: {},
          discovery: null,
          modelSearch: "",
        },
      };
    },
    computed: {
      navItems() {
        return [
          { key: "overview", label: "总览", hint: "快速掌握状态与接入路径" },
          { key: "upstreams", label: "上游节点", hint: "管理 Base URL、API Key 与模型列表" },
          { key: "keys", label: "代理 Key", hint: "生成给客户端使用的中转密钥" },
          { key: "requests", label: "调用明细", hint: "查看调用记录、Token 与耗时" },
          { key: "health", label: "健康测速", hint: "探测节点可用性与延迟" },
          { key: "settings", label: "设置", hint: "路由策略与全局参数" },
        ];
      },
      tabTitle() {
        const match = this.navItems.find((item) => item.key === this.activeTab);
        return match ? match.label : "总览";
      },
      tabDescription() {
        const match = this.navItems.find((item) => item.key === this.activeTab);
        return match ? match.hint : "";
      },
      enabledUpstreamCount() {
        return this.upstreams.filter((item) => item.enabled).length;
      },
      healthyUpstreamCount() {
        return this.healthItems.filter(
          (item) => item.health && item.health.last_probe_success === true && !item.health.timeout_failover_active,
        ).length;
      },
      uniqueModelCount() {
        const models = new Set();
        this.upstreams.forEach((item) => {
          this.parseModelsText(item.allowed_models_text).forEach((model) => models.add(model));
        });
        return models.size;
      },
      dialogModelList() {
        return this.parseModelsText(this.upstreamDialog.form.allowed_models_text);
      },
      filteredDialogModelList() {
        const keyword = (this.upstreamDialog.modelSearch || "").trim().toLowerCase();
        if (!keyword) {
          return this.dialogModelList;
        }
        return this.dialogModelList.filter((model) => model.toLowerCase().includes(keyword));
      },
      dialogDiscoveryTitle() {
        const discovery = this.upstreamDialog.discovery;
        if (!discovery) {
          return "";
        }
        if (discovery.source === "stored") {
          return `已载入上次探查结果，共 ${discovery.model_count} 个模型`;
        }
        return `探查成功，共 ${discovery.model_count} 个模型，耗时 ${this.formatLatency(discovery.latency_ms)}`;
      },
      dialogModelPanelTitle() {
        return this.upstreamDialog.discovery && this.upstreamDialog.discovery.source === "stored"
          ? "当前节点已保存模型"
          : "已识别模型列表";
      },
      dialogModelSourceLabel() {
        return this.upstreamDialog.discovery && this.upstreamDialog.discovery.source === "stored"
          ? "来源 已保存"
          : "来源 本次探查";
      },
      modelMappingModelOptions() {
        const models = new Set();
        [...this.upstreams, ...this.healthItems].forEach((item) => {
          if (Array.isArray(item.allowed_models)) {
            item.allowed_models.forEach((model) => {
              const normalized = String(model || "").trim();
              if (normalized) {
                models.add(normalized);
              }
            });
          }
          this.parseModelsText(item.allowed_models_text).forEach((model) => models.add(model));
        });
        Object.entries(this.settings.model_mappings || {}).forEach(([source, target]) => {
          if (source) {
            models.add(source);
          }
          if (target) {
            models.add(target);
          }
        });
        return Array.from(models).sort();
      },
    },
    mounted() {
      this.bootstrap();
      window.addEventListener("resize", this.resizeCharts);
    },
    beforeUnmount() {
      window.removeEventListener("resize", this.resizeCharts);
    },
    methods: {
      blankUpstream() {
        return {
          id: "",
          name: "",
          base_url: "",
          api_key: "",
          api_key_masked: "",
          order: this.upstreams.length + 1,
          timeout_ms: this.settings.request_timeout_ms || 120000,
          enabled: true,
          probe_enabled: true,
          endpoint_capabilities: ["*"],
          allowed_models_text: "",
          models_detected_at: null,
        };
      },
      buildStoredDiscovery(form) {
        const models = this.parseModelsText(form.allowed_models_text);
        if (!models.length) {
          return null;
        }
        return {
          models,
          model_count: models.length,
          discovered_at: form.models_detected_at || null,
          latency_ms: null,
          status_code: null,
          source: "stored",
        };
      },
      cloneObject(payload) {
        return JSON.parse(JSON.stringify(payload || {}));
      },
      normalizeUpstream(item, index) {
        return {
          id: item.id || "",
          name: item.name || "",
          base_url: item.base_url || "",
          api_key: "",
          api_key_masked: item.api_key_masked || "",
          order: item.order || index + 1,
          timeout_ms: item.timeout_ms || this.settings.request_timeout_ms || 120000,
          enabled: item.enabled !== false,
          probe_enabled: item.probe_enabled !== false,
          endpoint_capabilities: Array.isArray(item.endpoint_capabilities) && item.endpoint_capabilities.length
            ? item.endpoint_capabilities
            : ["*"],
          allowed_models: Array.isArray(item.allowed_models) ? item.allowed_models : [],
          allowed_models_text: Array.isArray(item.allowed_models) ? item.allowed_models.join("\n") : "",
          models_detected_at: item.models_detected_at || null,
        };
      },
      parseModelsText(value) {
        return String(value || "")
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean);
      },
      syncModelMappingRows() {
        this.modelMappingRows = Object.entries(this.settings.model_mappings || {}).map(([source, target]) => ({
          source,
          target,
        }));
      },
      buildModelMappings() {
        const mappings = {};
        this.modelMappingRows.forEach((row) => {
          const source = String(row.source || "").trim();
          const target = String(row.target || "").trim();
          if (!source || !target || source === target) {
            return;
          }
          mappings[source] = target;
        });
        return mappings;
      },
      addModelMapping() {
        this.modelMappingRows.push({ source: "", target: "" });
      },
      removeModelMapping(index) {
        this.modelMappingRows.splice(index, 1);
      },
      modelCount(item) {
        return this.parseModelsText(item.allowed_models_text).length;
      },
      previewModels(item, limit = 4) {
        return this.parseModelsText(item.allowed_models_text).slice(0, limit);
      },
      hiddenModelCount(item, limit = 4) {
        return Math.max(this.modelCount(item) - limit, 0);
      },
      orderedUpstreams() {
        return [...this.upstreams].sort((a, b) => (a.order || 9999) - (b.order || 9999));
      },
      routeModeLabel(mode) {
        return mode === "fastest_first" ? "按测速优选" : "有序故障切换";
      },
      healthSnapshot(item) {
        const found = this.healthItems.find((candidate) => candidate.id === item.id);
        return found ? found.health || {} : item.health || {};
      },
      probeJobItem(item) {
        if (!this.healthProbeJob || !this.healthProbeJob.items) {
          return null;
        }
        return this.healthProbeJob.items[item.id] || null;
      },
      isProbingItem(item) {
        const probeItem = this.probeJobItem(item);
        if (!probeItem) {
          return false;
        }
        return probeItem.status === "running" || probeItem.status === "pending";
      },
      healthTagType(item) {
        if (this.isProbingItem(item)) {
          return "warning";
        }
        const health = this.healthSnapshot(item);
        if (health.timeout_failover_active) {
          return "warning";
        }
        if (health.last_probe_success === true) {
          return "success";
        }
        if (health.last_probe_success === false) {
          return "danger";
        }
        return "info";
      },
      healthLabel(item) {
        if (this.isProbingItem(item)) {
          return "Probing";
        }
        const health = this.healthSnapshot(item);
        if (health.timeout_failover_active) {
          return "Failover";
        }
        if (health.last_probe_success === true) {
          return "Healthy";
        }
        if (health.last_probe_success === false) {
          return "Issue";
        }
        return "Unknown";
      },
      formatNumber(value) {
        return Number(value || 0).toLocaleString();
      },
      parseDateValue(value) {
        if (!value) {
          return null;
        }
        if (typeof value === "number") {
          return new Date(value);
        }
        const raw = String(value).trim();
        if (!raw) {
          return null;
        }
        if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(raw)) {
          return new Date(`${raw}:00+00:00`);
        }
        if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(raw)) {
          return new Date(`${raw}+00:00`);
        }
        const parsed = new Date(raw);
        if (Number.isNaN(parsed.getTime())) {
          return null;
        }
        return parsed;
      },
      formatDateTime(value, withSeconds = true) {
        const date = this.parseDateValue(value);
        if (!date) {
          return value || "-";
        }
        const formatter = new Intl.DateTimeFormat("zh-CN", {
          timeZone: "Asia/Shanghai",
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          second: withSeconds ? "2-digit" : undefined,
          hour12: false,
        });
        return formatter.format(date);
      },
      formatChartTime(value) {
        return this.formatDateTime(value, false);
      },
      formatLatency(value) {
        if (value === null || value === undefined || value === "") {
          return "-";
        }
        return `${Number(value).toFixed(0)} ms`;
      },
      formatPercent(value) {
        if (value === null || value === undefined || value === "") {
          return "-";
        }
        return `${Number(value).toFixed(2)}%`;
      },
      probeStepTrail(item) {
        const probeItem = this.probeJobItem(item);
        if (probeItem && probeItem.steps && probeItem.steps.length) {
          return probeItem.steps;
        }
        return (this.healthSnapshot(item).last_probe_steps || []).slice(-5);
      },
      currentProbeMessage(item) {
        const steps = this.probeStepTrail(item);
        if (!steps.length) {
          return "等待开始";
        }
        return steps[steps.length - 1].message;
      },
      probeStageLabel(stage) {
        const mapping = {
          models_request: "请求模型列表",
          models_validate: "校验模型列表",
          probe_prepare: "准备探活模型",
          probe_model: "选择探活模型",
          response_request: "发送 hi 请求",
          response_stream: "等待流式输出",
          response_validate: "校验回复内容",
          finished: "探活完成",
          exception: "异常",
        };
        return mapping[stage] || stage;
      },
      probeModelOptions(item) {
        const models = new Set();
        if (Array.isArray(item.allowed_models)) {
          item.allowed_models
            .map((model) => String(model || "").trim())
            .filter(Boolean)
            .forEach((model) => models.add(model));
        }
        this.parseModelsText(item.allowed_models_text).forEach((model) => models.add(model));
        const lastProbeModel = item.health && item.health.last_probe_model_used;
        if (lastProbeModel) {
          models.add(lastProbeModel);
        }
        models.add("gpt-5.4");
        return Array.from(models);
      },
      probeEndpointOptions(item) {
        const endpoints = new Set();
        const capabilities = Array.isArray(item.endpoint_capabilities) ? item.endpoint_capabilities : [];
        if (capabilities.includes("*") || capabilities.includes("chat.completions")) {
          endpoints.add("chat.completions");
        }
        if (capabilities.includes("*") || capabilities.includes("responses")) {
          endpoints.add("responses");
        }
        if (!endpoints.size) {
          endpoints.add("chat.completions");
          endpoints.add("responses");
        }
        return Array.from(endpoints);
      },
      selectedProbeModel(item) {
        return this.probeModelSelections[item.id] || "gpt-5.4";
      },
      syncProbeSelections() {
        const nextSelections = {};
        const nextEndpointSelections = {};
        const itemsById = new Map();
        [...this.upstreams, ...this.healthItems].forEach((item) => {
          if (item && item.id) {
            itemsById.set(item.id, item);
          }
        });
        itemsById.forEach((item) => {
          const previous = this.probeModelSelections[item.id];
          const modelOptions = this.probeModelOptions(item);
          nextSelections[item.id] = modelOptions.includes(previous)
            ? previous
            : modelOptions[0] || "gpt-5.4";
          const endpointOptions = this.probeEndpointOptions(item);
          const previousEndpoint = this.probeEndpointSelections[item.id];
          nextEndpointSelections[item.id] = endpointOptions.includes(previousEndpoint)
            ? previousEndpoint
            : endpointOptions[0];
        });
        this.probeModelSelections = nextSelections;
        this.probeEndpointSelections = nextEndpointSelections;
      },
      upstreamPriorityPreview(item) {
        return `${item.order || 0}. ${item.name}`;
      },
      async api(path, options = {}) {
        const method = (options.method || "GET").toUpperCase();
        const headers = Object.assign({}, options.headers || {});
        if (!headers["Content-Type"] && method !== "GET" && method !== "HEAD") {
          headers["Content-Type"] = "application/json";
        }
        if (this.session.csrf_token && !["GET", "HEAD"].includes(method)) {
          headers["X-CSRF-Token"] = this.session.csrf_token;
        }
        const response = await fetch(path, {
          credentials: "same-origin",
          ...options,
          headers,
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload?.error?.message || "请求失败");
        }
        return payload;
      },
      async bootstrap() {
        await this.checkSession();
        if (this.session.authenticated) {
          await this.refreshAll();
        }
      },
      async checkSession() {
        try {
          const payload = await this.api("/admin/api/session", { method: "GET" });
          this.session = {
            authenticated: !!payload.authenticated,
            username: payload.username || "",
            csrf_token: payload.csrf_token || "",
          };
        } catch (_error) {
          this.session = { authenticated: false, username: "", csrf_token: "" };
        }
      },
      async submitLogin() {
        this.loading.session = true;
        this.loginError = "";
        try {
          const payload = await this.api("/admin/api/session", {
            method: "POST",
            body: JSON.stringify(this.loginForm),
          });
          this.session = payload;
          await this.refreshAll();
        } catch (error) {
          this.loginError = error.message;
        } finally {
          this.loading.session = false;
        }
      },
      async logout() {
        try {
          await this.api("/admin/api/session", { method: "DELETE" });
        } catch (_error) {
        }
        this.session = { authenticated: false, username: "", csrf_token: "" };
      },
      async refreshAll() {
        if (!this.session.authenticated) {
          return;
        }
        this.loading.refresh = true;
        try {
          await Promise.all([
            this.loadSettings(),
            this.loadUpstreams(),
            this.loadProxyKeys(),
            this.loadHealth(),
            this.loadRequests(),
            this.refreshStats(),
          ]);
        } finally {
          this.loading.refresh = false;
        }
      },
      handleTabChange(tab) {
        this.activeTab = tab;
        if (tab === "overview") {
          nextTick(() => this.renderCharts());
        }
      },
      async loadSettings() {
        const payload = await this.api("/admin/api/settings");
        this.settings = { ...this.settings, ...payload.item };
        this.syncModelMappingRows();
        this.pageTitle = this.settings.title || this.pageTitle;
      },
      async saveSettings() {
        this.loading.settings = true;
        try {
          const settingsPayload = {
            ...this.settings,
            model_mappings: this.buildModelMappings(),
          };
          const payload = await this.api("/admin/api/settings", {
            method: "PUT",
            body: JSON.stringify(settingsPayload),
          });
          this.settings = payload.item;
          this.syncModelMappingRows();
          this.pageTitle = payload.item.title;
          message.success("设置已保存");
        } finally {
          this.loading.settings = false;
        }
      },
      async loadUpstreams() {
        const payload = await this.api("/admin/api/upstreams");
        this.upstreams = payload.items.map((item, index) => this.normalizeUpstream(item, index));
        this.syncProbeSelections();
      },
      handleUpstreamDragStart(item) {
        this.draggingUpstreamId = item.id;
      },
      handleUpstreamDragEnd() {
        this.draggingUpstreamId = null;
      },
      allowUpstreamDrop(event) {
        event.preventDefault();
      },
      async handleUpstreamDrop(targetItem) {
        if (!this.draggingUpstreamId || this.draggingUpstreamId === targetItem.id) {
          this.draggingUpstreamId = null;
          return;
        }
        const ordered = this.orderedUpstreams();
        const fromIndex = ordered.findIndex((item) => item.id === this.draggingUpstreamId);
        const toIndex = ordered.findIndex((item) => item.id === targetItem.id);
        if (fromIndex < 0 || toIndex < 0) {
          this.draggingUpstreamId = null;
          return;
        }
        const [moved] = ordered.splice(fromIndex, 1);
        ordered.splice(toIndex, 0, moved);
        const nextItems = ordered.map((item, index) => ({
          ...item,
          order: index + 1,
        }));
        this.draggingUpstreamId = null;
        await this.persistUpstreams(nextItems, "优先级顺序已更新");
      },
      openCreateUpstream() {
        this.upstreamDialog = {
          visible: true,
          mode: "create",
          form: this.blankUpstream(),
          discovery: null,
          modelSearch: "",
        };
      },
      openEditUpstream(item) {
        const form = this.normalizeUpstream(item, this.upstreams.findIndex((entry) => entry.id === item.id));
        this.upstreamDialog = {
          visible: true,
          mode: "edit",
          form,
          discovery: this.buildStoredDiscovery(form),
          modelSearch: "",
        };
      },
      async discoverUpstream(payload, silent) {
        this.loading.discoverModels = true;
        try {
          const response = await this.api("/admin/api/upstreams/discover", {
            method: "POST",
            body: JSON.stringify(payload),
          });
          if (!silent) {
            message.success(`探查成功，识别到 ${response.item.model_count} 个模型`);
          }
          return response.item;
        } finally {
          this.loading.discoverModels = false;
        }
      },
      async discoverDialogModels(silent = false) {
        const form = this.upstreamDialog.form;
        if (!form.base_url.trim()) {
          message.error("请先填写 Base URL");
          return null;
        }
        const discovery = await this.discoverUpstream(
          {
            id: form.id,
            base_url: form.base_url,
            api_key: form.api_key,
            timeout_ms: form.timeout_ms,
          },
          silent,
        );
        this.upstreamDialog.discovery = discovery;
        this.upstreamDialog.form.allowed_models_text = discovery.models.join("\n");
        this.upstreamDialog.form.models_detected_at = discovery.discovered_at;
        this.upstreamDialog.modelSearch = "";
        return discovery;
      },
      async copyDialogModels() {
        const text = this.dialogModelList.join("\n");
        if (!text) {
          message.warning("当前没有可复制的模型列表");
          return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
          message.success("模型列表已复制");
          return;
        }
        message.warning("当前浏览器环境不支持自动复制");
      },
      buildUpstreamPayload(item, index) {
        const allowedModels = this.parseModelsText(item.allowed_models_text);
        return {
          id: item.id,
          name: item.name,
          base_url: item.base_url,
          api_key: item.api_key,
          order: item.order || index + 1,
          timeout_ms: item.timeout_ms,
          enabled: item.enabled,
          probe_enabled: item.probe_enabled,
          endpoint_capabilities: item.endpoint_capabilities,
          allowed_models: allowedModels,
          models_detected_at: item.models_detected_at,
          auto_discover_models: false,
        };
      },
      async persistUpstreams(items, successMessage) {
        const upstreams = items.map((item, index) => this.buildUpstreamPayload(item, index));
        await this.api("/admin/api/upstreams", {
          method: "PUT",
          body: JSON.stringify({ upstreams }),
        });
        await Promise.all([this.loadUpstreams(), this.loadHealth()]);
        if (successMessage) {
          message.success(successMessage);
        }
      },
      async saveUpstreamDialog() {
        const form = this.upstreamDialog.form;
        if (!form.name.trim()) {
          message.error("请填写节点名称");
          return;
        }
        if (!form.base_url.trim()) {
          message.error("请填写 Base URL");
          return;
        }
        if (!form.id && !form.api_key.trim()) {
          message.error("新增节点时必须填写 API Key");
          return;
        }
        this.loading.upstreamDialog = true;
        try {
          const draft = this.cloneObject(this.upstreamDialog.form);
          let nextItems;
          if (this.upstreamDialog.mode === "edit") {
            nextItems = this.upstreams.map((item) => (item.id === draft.id ? draft : item));
          } else {
            nextItems = [...this.upstreams, draft];
          }
          await this.persistUpstreams(nextItems, "节点已保存并生效");
          this.upstreamDialog.visible = false;
        } finally {
          this.loading.upstreamDialog = false;
        }
      },
      async quickDiscoverUpstream(item) {
        const discovery = await this.discoverUpstream(
          {
            id: item.id,
            base_url: item.base_url,
            timeout_ms: item.timeout_ms,
          },
          false,
        );
        const nextItems = this.upstreams.map((entry) => {
          if (entry.id !== item.id) {
            return entry;
          }
          return {
            ...entry,
            allowed_models_text: discovery.models.join("\n"),
            models_detected_at: discovery.discovered_at,
          };
        });
        await this.persistUpstreams(nextItems, "模型列表已更新");
      },
      async removeUpstream(item) {
        await messageBox.confirm(
          `确定删除节点“${item.name}”吗？`,
          "删除确认",
          { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
        );
        const nextItems = this.upstreams.filter((entry) => entry.id !== item.id);
        await this.persistUpstreams(nextItems, "节点已删除");
      },
      async loadProxyKeys() {
        const payload = await this.api("/admin/api/proxy-keys");
        this.proxyKeys = payload.items;
      },
      async createProxyKey() {
        if (!this.newKey.name.trim()) {
          message.error("请填写 Key 名称");
          return;
        }
        this.loading.keys = true;
        try {
          const payload = await this.api("/admin/api/proxy-keys", {
            method: "POST",
            body: JSON.stringify(this.newKey),
          });
          this.newlyCreatedKey = payload.plaintext_key;
          this.newKey = { name: "", note: "" };
          await this.loadProxyKeys();
          message.success("代理 Key 已创建");
        } finally {
          this.loading.keys = false;
        }
      },
      async toggleProxyKey(item) {
        const previous = !item.enabled;
        try {
          await this.api(`/admin/api/proxy-keys/${item.id}`, {
            method: "PATCH",
            body: JSON.stringify({ enabled: item.enabled }),
          });
          message.success("Key 状态已更新");
        } catch (error) {
          item.enabled = previous;
          message.error(error.message);
        }
      },
      async loadRequests() {
        const params = new URLSearchParams();
        Object.entries(this.requestFilters).forEach(([key, value]) => {
          if (value !== "" && value !== null && value !== undefined) {
            params.set(key, value);
          }
        });
        const payload = await this.api(`/admin/api/requests?${params.toString()}`);
        this.requests = payload;
      },
      handleRequestPage(page) {
        this.requestFilters.page = page;
        this.loadRequests();
      },
      handleRequestPageSize(size) {
        this.requestFilters.page_size = size;
        this.requestFilters.page = 1;
        this.loadRequests();
      },
      async refreshStats() {
        const payload = await this.api(`/admin/api/stats?days=${this.statsDays}`);
        this.stats = payload;
        if (this.activeTab === "overview") {
          await nextTick();
          this.renderCharts();
        }
      },
      async loadHealth() {
        const payload = await this.api("/admin/api/health");
        this.healthItems = payload.items.map((item, index) => ({
          ...this.normalizeUpstream(item, index),
          health: item.health || {},
        }));
        this.syncProbeSelections();
      },
      async startProbeJob(upstreamId = null) {
        this.loading.health = true;
        try {
          const payloadBody = upstreamId
            ? {
                upstream_id: upstreamId,
                probe_model: this.probeModelSelections[upstreamId] || "gpt-5.4",
                probe_endpoint: this.probeEndpointSelections[upstreamId] || "chat.completions",
              }
            : {
                probe_models: Object.fromEntries(
                  this.upstreams.map((item) => [item.id, this.probeModelSelections[item.id] || "gpt-5.4"]),
                ),
                probe_endpoints: Object.fromEntries(
                  this.upstreams.map((item) => [item.id, this.probeEndpointSelections[item.id] || "chat.completions"]),
                ),
              };
          const payload = await this.api("/admin/api/health/probe/jobs", {
            method: "POST",
            body: JSON.stringify(payloadBody),
          });
          this.healthProbeJob = payload.item;
          await this.pollProbeJob(payload.item.id);
        } catch (error) {
          this.loading.health = false;
          message.error(error.message);
        }
      },
      async pollProbeJob(jobId) {
        while (true) {
          const payload = await this.api(`/admin/api/health/probe/jobs/${jobId}`);
          this.healthProbeJob = payload.item;
          if (payload.item.status === "completed") {
            break;
          }
          await new Promise((resolve) => window.setTimeout(resolve, 700));
        }
        await this.loadHealth();
        this.loading.health = false;
        const failedCount = Object.values(this.healthProbeJob.items || {}).filter(
          (item) => item.status === "failed",
        ).length;
        if (failedCount > 0) {
          message.warning(`探活完成，其中 ${failedCount} 个节点失败`);
        } else {
          message.success("探活完成，所有节点健康");
        }
      },
      async probeAll() {
        await this.startProbeJob();
      },
      async probeOne(upstreamId) {
        await this.startProbeJob(upstreamId);
      },
      getChart(id) {
        const element = document.getElementById(id);
        if (!element) {
          return null;
        }
        if (!this.charts[id]) {
          this.charts[id] = echarts.init(element);
        }
        return this.charts[id];
      },
      renderCharts() {
        const requestsChart = this.getChart("requests-chart");
        const statusChart = this.getChart("status-chart");
        const modelsChart = this.getChart("models-chart");
        const latencyChart = this.getChart("latency-chart");

        if (requestsChart) {
          requestsChart.setOption({
            tooltip: { trigger: "axis" },
            grid: { left: 38, right: 20, top: 20, bottom: 40 },
            xAxis: {
              type: "category",
              data: this.stats.timeseries.map((item) => this.formatChartTime(item.time)),
              axisLine: { lineStyle: { color: "#bfd0c2" } },
            },
            yAxis: {
              type: "value",
              splitLine: { lineStyle: { color: "rgba(17,33,24,0.08)" } },
            },
            series: [{
              data: this.stats.timeseries.map((item) => item.count),
              type: "line",
              smooth: true,
              areaStyle: { color: "rgba(15,140,96,0.16)" },
              lineStyle: { color: "#0f8c60", width: 3 },
              symbol: "circle",
            }],
          });
        }

        if (statusChart) {
          statusChart.setOption({
            tooltip: { trigger: "item" },
            series: [{
              type: "pie",
              radius: ["50%", "74%"],
              itemStyle: { borderRadius: 12 },
              data: this.stats.status_breakdown,
            }],
          });
        }

        if (modelsChart) {
          modelsChart.setOption({
            tooltip: { trigger: "axis" },
            grid: { left: 100, right: 24, top: 20, bottom: 20 },
            xAxis: { type: "value" },
            yAxis: {
              type: "category",
              data: this.stats.top_models.map((item) => item.name),
              axisTick: { show: false },
            },
            series: [{
              type: "bar",
              data: this.stats.top_models.map((item) => item.value),
              itemStyle: { color: "#1b63d0", borderRadius: [0, 10, 10, 0] },
            }],
          });
        }

        if (latencyChart) {
          latencyChart.setOption({
            tooltip: { trigger: "axis" },
            grid: { left: 46, right: 20, top: 20, bottom: 40 },
            xAxis: {
              type: "category",
              data: this.stats.latency_by_upstream.map((item) => item.name),
            },
            yAxis: { type: "value" },
            series: [{
              type: "bar",
              data: this.stats.latency_by_upstream.map((item) => item.value),
              itemStyle: { color: "#0f8c60", borderRadius: [10, 10, 0, 0] },
            }],
          });
        }
      },
      resizeCharts() {
        Object.values(this.charts).forEach((chart) => {
          if (chart) {
            chart.resize();
          }
        });
      },
    },
  }).use(ElementPlus).mount("#app");
})();
