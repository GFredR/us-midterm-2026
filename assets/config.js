/* ==========================================================================
   站点数据源配置
   --------------------------------------------------------------------------
   local  —— 与站点同源的数据文件（部署时的那份快照，或 server.py 自己维护的快照）
   remote —— GitHub Actions 每 30 分钟在美国 IP 采集后提交到仓库的最新版快照
   --------------------------------------------------------------------------
   两份都拉到时，取 meta.generated_at 较新的一份。
   想换成自己的仓库，改 remote 即可。
   ========================================================================== */
window.SITE_CONFIG = {
  local: 'data/election-2026.json',
  remote: 'https://raw.githubusercontent.com/GFredR/us-midterm-2026/main/data/election-2026.json',
  // 自动刷新间隔。CI 每 30 分钟产一版快照，刷新太勤没有意义。
  autoRefreshMs: 5 * 60 * 1000,
  serverStatusUrl: '/api/status',
  serverRefreshUrl: '/api/refresh'
};
