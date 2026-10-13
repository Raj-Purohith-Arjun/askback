/* Bootstrap: tabs, theme, the engine status chip, and starting the panels.
 *
 * Loaded last. Everything it uses comes from core.js and panels.js.
 */

const App = {
  panels: {
    console: ConsolePanel,
    evaluation: EvaluationPanel,
    knowledge: KnowledgePanel,
    routing: RoutingPanel,
  },

  current: null,

  init() {
    this.initTheme();

    $('tabs').addEventListener('click', (event) => {
      const tab = event.target.closest('.tab');
      if (tab) this.show(tab.dataset.panel);
    });

    $('reload-engine').addEventListener('click', guard(async (event) =>
      withBusy(event.target, '…', async () => {
        await api.post('/api/system/reload', {});
        toast('Engine reloaded from disk', 'success');
        await this.refreshEngineChip();
        // The corpus may have changed underneath these two, so re-read them.
        KnowledgePanel.loaded = false;
        RoutingPanel.loaded = false;
      })));

    for (const panel of Object.values(this.panels)) panel.init();

    // Deep-linkable: /#evaluation opens that tab directly, and pasting a new
    // hash into an already-open tab switches panel rather than doing nothing.
    const fromHash = () => window.location.hash.replace('#', '');
    window.addEventListener('hashchange', () => {
      const name = fromHash();
      if (this.panels[name]) this.show(name);
    });
    this.show(this.panels[fromHash()] ? fromHash() : 'console');

    this.refreshEngineChip();
  },

  show(name) {
    if (this.current === name) return;
    this.current = name;
    window.history.replaceState(null, '', `#${name}`);

    for (const tab of $('tabs').children) {
      tab.classList.toggle('active', tab.dataset.panel === name);
    }
    for (const [key, panel] of Object.entries(this.panels)) {
      $(`panel-${key}`).classList.toggle('active', key === name);
      if (key === name) guard(() => panel.activate())();
    }
  },

  /** Theme follows the OS until the user picks one, then the choice sticks. */
  initTheme() {
    const stored = localStorage.getItem('askback-theme');
    if (stored) document.documentElement.dataset.theme = stored;

    $('theme-toggle').addEventListener('click', () => {
      const root = document.documentElement;
      const isDark = root.dataset.theme
        ? root.dataset.theme === 'dark'
        : window.matchMedia('(prefers-color-scheme: dark)').matches;
      const next = isDark ? 'light' : 'dark';
      root.dataset.theme = next;
      localStorage.setItem('askback-theme', next);
    });
  },

  /** The top-right chip: what the engine is actually wired to right now. */
  async refreshEngineChip() {
    try {
      const info = await api.get('/api/system/info');
      $('engine-dot').className = 'dot ok';
      $('engine-summary').textContent =
        `${info.llm} · ${info.retriever} · ${info.agents.length} agents`;
      $('engine-chip').title =
        `LLM: ${info.llm}\nRetriever: ${info.retriever}\n`
        + `Agents: ${info.agents.join(', ')}\nTools: ${info.tools.join(', ')}\n`
        + `Guards: ${info.guards.join(', ')}\nReloads: ${info.reload_count}`;
    } catch (error) {
      $('engine-dot').className = 'dot bad';
      $('engine-summary').textContent = 'engine unavailable';
      $('engine-chip').title = error.message;
    }
  },
};

window.App = App;
document.addEventListener('DOMContentLoaded', () => App.init());
