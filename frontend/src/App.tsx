import { useCallback, useEffect, useMemo, useState } from 'react'
import ForecastChart from './components/ForecastChart'
import { api, Job } from './lib/api'

type Candle = { timestamp: string; open: number; high: number; low: number; close: number; volume: number }
type Tab = 'overview' | 'scanner' | 'forecast' | 'analyze' | 'backtest' | 'signals' | 'risk' | 'settings'

const nav: { id: Tab; label: string; icon: string }[] = [
  { id: 'overview', label: 'Overview', icon: '⌂' }, { id: 'scanner', label: 'Market scanner', icon: '⌕' },
  { id: 'forecast', label: 'Forecast desk', icon: '⌁' }, { id: 'analyze', label: 'Analyze', icon: '◈' },
  { id: 'backtest', label: 'Backtesting', icon: '▥' }, { id: 'signals', label: 'Signals', icon: '◉' },
  { id: 'risk', label: 'Risk desk', icon: '⛨' }, { id: 'settings', label: 'Settings', icon: '⚙' },
]

function money(value?: number | null) {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 4 })
}
function pct(value?: number | null) { return value == null ? '—' : `${(Number(value) * 100).toFixed(1)}%` }
function statusClass(value?: string) {
  const v = (value || '').toLowerCase()
  if (v.includes('approved') || v.includes('completed') || v.includes('ready') || v.includes('bull')) return 'positive'
  if (v.includes('failed') || v.includes('reject') || v.includes('bear') || v.includes('blocked')) return 'negative'
  return 'neutral'
}

function Login({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setError('')
    try { await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }); onLogin() }
    catch (err) { setError(err instanceof Error ? err.message : 'Login failed') }
    finally { setBusy(false) }
  }
  return <div className="login-shell"><form className="login-card" onSubmit={submit}>
    <div className="brand-mark">K<span>✳</span></div><div className="eyebrow">PAK TRADING ACADEMY</div>
    <h1>KRONOS <em>INTELLIGENCE</em></h1><p className="muted">Private quantitative research terminal</p>
    <label>Username<input value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" /></label>
    <label>Password<input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" /></label>
    {error && <div className="error-line">{error}</div>}
    <button className="primary-button full" disabled={busy}>{busy ? 'Checking…' : 'Sign in to terminal'}</button>
    <small>Use the ADMIN_USERNAME and ADMIN_PASSWORD configured by the operator.</small>
  </form></div>
}

export default function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null)
  const [tab, setTab] = useState<Tab>('overview')
  const [symbol, setSymbol] = useState('BTC_USDT')
  const [status, setStatus] = useState<any>(null)
  const [market, setMarket] = useState<any>(null)
  const [candles, setCandles] = useState<Candle[]>([])
  const [universe, setUniverse] = useState<any[]>([])
  const [signals, setSignals] = useState<any[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [forecasts, setForecasts] = useState<any[]>([])
  const [currentForecast, setCurrentForecast] = useState<any>(null)
  const [backtests, setBacktests] = useState<any[]>([])
  const [selectedBacktest, setSelectedBacktest] = useState<any>(null)
  const [analysis, setAnalysis] = useState<any>(null)
  const [activeJob, setActiveJob] = useState<Job | null>(null)
  const [showWaves, setShowWaves] = useState(true)
  const [showBands, setShowBands] = useState(true)
  const [days, setDays] = useState(30)
  const [useKronosFilter, setUseKronosFilter] = useState(false)
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [s, sig, j, f, b] = await Promise.all([
        api('/api/status'), api('/api/signals?limit=30'), api('/api/jobs?limit=12'),
        api(`/api/forecasts?symbol=${encodeURIComponent(symbol)}&limit=10`), api('/api/backtests?limit=10'),
      ])
      setStatus(s); setSignals(sig.items || []); setJobs(j.items || []); setForecasts(f.items || []); setBacktests(b.items || [])
    } catch (err) { if (String(err).includes('AUTH_REQUIRED')) setAuthenticated(false) }
  }, [symbol])

  const loadMarket = useCallback(async () => {
    setLoading(true)
    try {
      const [m, c] = await Promise.all([
        api(`/api/market/${encodeURIComponent(symbol)}`),
        api(`/api/market/${encodeURIComponent(symbol)}/candles?interval=Min60&limit=160`),
      ])
      setMarket(m); setCandles(c.items || [])
    } catch (err) { setNotice(err instanceof Error ? err.message : 'Market data unavailable') }
    finally { setLoading(false) }
  }, [symbol])

  useEffect(() => {
    api('/api/auth/me').then(() => setAuthenticated(true)).catch(() => setAuthenticated(false))
  }, [])
  useEffect(() => { if (authenticated) { refresh(); loadMarket(); api('/api/universe').then(x => setUniverse(x.items || [])).catch(() => {}) } }, [authenticated, refresh, loadMarket])
  useEffect(() => { if (!authenticated) return; const id = window.setInterval(refresh, 5000); return () => window.clearInterval(id) }, [authenticated, refresh])
  useEffect(() => {
    if (!activeJob || ['completed', 'failed', 'cancelled'].includes(activeJob.status)) return
    const id = window.setInterval(async () => {
      try {
        const job = await api<Job>(`/api/jobs/${activeJob.id}`); setActiveJob(job)
        if (job.status === 'completed') {
          if (job.type === 'analysis') setAnalysis(job.result)
          if (job.type === 'backtest') { setSelectedBacktest(job.result); refresh() }
          if (job.type === 'forecast') refresh()
          setNotice(`${job.type} job completed`)
        }
      } catch (err) { setNotice(err instanceof Error ? err.message : 'Job polling failed') }
    }, 2000)
    return () => window.clearInterval(id)
  }, [activeJob, refresh])

  async function action(path: string, body: any, label: string) {
    setNotice(`${label} queued`)
    try { const job = await api<Job>(path, { method: 'POST', body: JSON.stringify(body) }); setActiveJob(job); setNotice(`${label} queued · ${job.id.slice(0, 8)}`) }
    catch (err) { setNotice(err instanceof Error ? err.message : `${label} failed`) }
  }

  async function loadForecast(id: string) {
    try { const f = await api(`/api/forecasts/${id}?include_paths=true`); setCurrentForecast(f); setTab('forecast') }
    catch (err) { setNotice(err instanceof Error ? err.message : 'Forecast load failed') }
  }
  async function loadReport(id: string) {
    try { setSelectedBacktest(await api(`/api/backtests/${id}`)); setTab('backtest') }
    catch (err) { setNotice(err instanceof Error ? err.message : 'Report load failed') }
  }
  async function logout() { await api('/api/auth/logout', { method: 'POST' }).catch(() => {}); setAuthenticated(false) }

  const latestSignal = signals[0]
  const latestForecast = forecasts[0]
  const marketSpread = market?.ticker?.bid1 && market?.ticker?.ask1 ? ((market.ticker.ask1 - market.ticker.bid1) / ((market.ticker.ask1 + market.ticker.bid1) / 2) * 10000) : null
  const forecastPaths = currentForecast?.paths || []
  const metrics = selectedBacktest?.metrics

  if (authenticated === null) return <div className="boot-screen">Loading secure workspace…</div>
  if (!authenticated) return <Login onLogin={() => setAuthenticated(true)} />

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="side-brand"><div className="brand-mark small">K<span>✳</span></div><div><b>KRONOS</b><small>INTELLIGENCE</small></div></div>
      <div className="side-section">WORKSPACE</div>
      <nav>{nav.map(n => <button key={n.id} className={`nav-item ${tab === n.id ? 'active' : ''}`} onClick={() => setTab(n.id)}><span className="nav-icon">{n.icon}</span>{n.label}{n.id === 'signals' && signals.length > 0 && <span className="nav-count">{signals.length}</span>}</button>)}</nav>
      <div className="side-bottom">
        <div className="system-chip"><span className="pulse-dot" /> API workspace <b>ONLINE</b></div>
        <div className="side-user"><div className="avatar">PT</div><div><b>PAK TRADING ACADEMY</b><small>Administrator</small></div><button onClick={logout} title="Sign out">↗</button></div>
      </div>
    </aside>

    <main className="main-area">
      <header className="topbar">
        <div><div className="eyebrow">QUANTITATIVE RESEARCH TERMINAL</div><h2>{nav.find(x => x.id === tab)?.label}</h2></div>
        <div className="top-actions"><div className="connection"><span className="pulse-dot" /> MEXC DATA</div><div className="symbol-picker"><span>CONTRACT</span><input value={symbol} onChange={e => setSymbol(e.target.value.toUpperCase())} onBlur={loadMarket} onKeyDown={e => e.key === 'Enter' && loadMarket()} /></div><button className="icon-button" onClick={() => { refresh(); loadMarket() }} title="Refresh">⟳</button></div>
      </header>
      {notice && <div className="notice-bar"><span>{notice}</span><button onClick={() => setNotice('')}>×</button></div>}
      {activeJob && !['completed', 'failed', 'cancelled'].includes(activeJob.status) && <div className="job-progress"><div><b>{activeJob.type.toUpperCase()}</b><span>{activeJob.progress_message}</span></div><div className="progress-track"><i style={{ width: `${activeJob.progress}%` }} /></div><small>{Math.round(activeJob.progress)}%</small></div>}

      {tab === 'overview' && <>
        <div className="hero-row"><div><div className="eyebrow">MEXC USDT-M PERPETUALS · LIVE MARKET SNAPSHOT</div><h1>Market intelligence,<br /><em>without the guesswork.</em></h1><p>Model forecasts, historical scenarios and transparent risk controls in one research terminal.</p><div className="hero-buttons"><button className="primary-button" onClick={() => action('/api/scan', { confirm: true }, 'Market scan')}>⌕ Run market scan</button><button className="secondary-button" onClick={() => action('/api/analyze', { symbol }, 'Analysis')}>◈ Analyze {symbol}</button></div></div><div className="hero-emblem"><div className="orbit orbit-one"/><div className="orbit orbit-two"/><div className="emblem-core">K<span>✳</span></div><div className="orbit-label label-a">FORECAST</div><div className="orbit-label label-b">SIMULATE</div><div className="orbit-label label-c">VALIDATE</div></div></div>
        <div className="metric-grid">
          <Metric label="LAST PRICE" value={`$${money(market?.ticker?.lastPrice)}`} sub={symbol} accent="green" />
          <Metric label="24H TURNOVER" value={`$${compact(market?.ticker?.amount24)}`} sub="MEXC Futures" />
          <Metric label="OPEN INTEREST" value={compact(market?.ticker?.holdVol)} sub="Contracts" />
          <Metric label="FUNDING RATE" value={market?.ticker?.fundingRate != null ? `${(market.ticker.fundingRate * 100).toFixed(4)}%` : '—'} sub="Current snapshot" />
        </div>
        <div className="content-grid wide-left"><section className="panel chart-panel"><div className="panel-head"><div><div className="eyebrow">FORECAST VISUALIZATION</div><h3>{symbol} <span className="muted-tag">1H</span></h3></div><div className="panel-actions"><button className="text-button" onClick={() => setTab('forecast')}>Open forecast desk ↗</button></div></div><ForecastChart candles={candles} paths={forecastPaths} summary={currentForecast?.summary} showWaves={showWaves} showBands={showBands} /></section>
          <section className="panel"><PanelTitle title="Latest paper signal" subtitle="Research candidate feed" /><div className="signal-feature">{latestSignal ? <><div className={`signal-direction ${latestSignal.side === 'LONG' ? 'long' : 'short'}`}>{latestSignal.side} · {latestSignal.symbol}</div><div className="signal-levels"><Level label="ENTRY" value={money(latestSignal.entry)} /><Level label="STOP" value={money(latestSignal.stop)} /><Level label="TP1" value={money(latestSignal.tp1)} /><Level label="TP2" value={money(latestSignal.tp2)} /></div><span className={`status-pill ${statusClass(latestSignal.status)}`}>{latestSignal.status}</span></> : <Empty text="No validated paper signals yet. Run a market scan to evaluate eligible contracts." />}</div></section></div>
        <div className="content-grid"><section className="panel"><PanelTitle title="Market scanner" subtitle="Highest-turnover eligible contracts" action={<button className="text-button" onClick={() => setTab('scanner')}>View all ↗</button>} /><MarketTable rows={universe.slice(0, 7)} onSelect={s => { setSymbol(s); setTab('analyze') }} /></section><section className="panel"><PanelTitle title="System health" subtitle="Deployment and model readiness" /><HealthRows status={status} /></section></div>
        <div className="footer-note">Research and paper trading only · Forecast path shares are not calibrated market probabilities · No live order execution</div>
      </>}

      {tab === 'scanner' && <div className="page-stack"><div className="page-intro"><div><div className="eyebrow">UNIVERSE · TURNOVER · LIQUIDITY</div><h1>Market scanner</h1><p>Eligible USDT perpetual contracts ranked by turnover, then filtered for spread and contract status.</p></div><button className="primary-button" onClick={() => action('/api/scan', { confirm: true }, 'Market scan')}>⌕ Scan universe</button></div><section className="panel"><MarketTable rows={universe} onSelect={s => { setSymbol(s); setTab('analyze') }} large /></section><section className="panel"><PanelTitle title="Recent scan jobs" subtitle="Worker execution log" /><JobTable jobs={jobs.filter(j => j.type === 'scan')} /></section></div>}

      {tab === 'forecast' && <div className="page-stack"><div className="page-intro"><div><div className="eyebrow">KRONOS · INDIVIDUAL PATH DECODING</div><h1>Forecast desk</h1><p>Explore the preserved path ensemble and percentile bands. Path share is not a calibrated probability.</p></div><div className="button-pair"><button className="secondary-button" onClick={() => setShowWaves(v => !v)}>{showWaves ? 'Hide' : 'Show'} waves</button><button className="secondary-button" onClick={() => setShowBands(v => !v)}>{showBands ? 'Hide' : 'Show'} bands</button><button className="primary-button" onClick={() => action('/api/forecast', { symbol, timeframe: 'Min60', samples: 1000, horizon: 24 }, 'Kronos forecast')}>✳ Generate 1,000 paths</button></div></div>
        <section className="panel chart-panel forecast-large"><div className="panel-head"><div><div className="eyebrow">{currentForecast?.metadata?.model_id || 'KRONOS-SMALL'}</div><h3>{symbol} <span className="muted-tag">1H · NEXT {currentForecast?.horizon || 24} BARS</span></h3></div><div className="tag-cluster"><span className="tag">{currentForecast?.sample_count || 0} PATHS LOADED</span><span className="tag">P10–P90</span></div></div><ForecastChart candles={candles} paths={forecastPaths} summary={currentForecast?.summary} showWaves={showWaves} showBands={showBands} /></section>
        {currentForecast?.summary && <div className="metric-grid"><Metric label="BULLISH PATH SHARE" value={pct(currentForecast.summary.bullish_path_share)} sub="Not a market probability" accent="green" /><Metric label="BEARISH PATH SHARE" value={pct(currentForecast.summary.bearish_path_share)} sub="Not a market probability" accent="red" /><Metric label="P50 FINAL RETURN" value={pct(currentForecast.summary.return_quantiles?.p50)} sub="Forecast horizon" /><Metric label="MODEL DEVICE" value={currentForecast.metadata?.device || '—'} sub={currentForecast.metadata?.model_revision?.slice(0, 12) || ''} /></div>}
        <section className="panel"><PanelTitle title="Recent forecasts" subtitle="Select a saved forecast to load all individual paths" /><div className="list-rows">{forecasts.map(f => <button className="list-row" key={f.id} onClick={() => loadForecast(f.id)}><div><b>{f.symbol}</b><small>{new Date(f.created_at).toLocaleString()} · {f.model_id}</small></div><div className="list-row-right"><span>{f.sample_count.toLocaleString()} paths · {f.horizon} bars</span><b>{pct(f.summary?.bullish_path_share)} bullish</b></div></button>)}{!forecasts.length && <Empty text="No forecasts stored yet." />}</div></section></div>}

      {tab === 'analyze' && <div className="page-stack"><div className="page-intro"><div><div className="eyebrow">1D → 12H → 4H → 1H</div><h1>Deep symbol analysis</h1><p>Completed-candle market structure, Kronos forecast, historical scenarios and paper risk estimate.</p></div><button className="primary-button" onClick={() => action('/api/analyze', { symbol }, 'Symbol analysis')}>◈ Analyze {symbol}</button></div>
        {analysis ? <><div className="decision-banner"><div><small>FINAL RESEARCH DECISION</small><h2>{analysis.decision}</h2><p>{analysis.reasons?.join(' · ')}</p></div><span className={`status-pill ${statusClass(analysis.decision)}`}>PAPER ONLY</span></div><div className="content-grid"><section className="panel"><PanelTitle title="Multi-timeframe alignment" subtitle="Only completed bars are evaluated" /><div className="timeframe-grid">{Object.entries(analysis.timeframes || {}).map(([tf, value]: any) => <div className="timeframe-card" key={tf}><small>{tf}</small><b>{value.bars} bars</b><span>${money(value.last_close)}</span></div>)}</div><div className="kv-list"><KV label="1D regime" value={analysis.strategy?.regime_1d} /><KV label="12H direction" value={analysis.strategy?.direction_12h} /><KV label="4H structure" value={analysis.strategy?.structure_4h?.trend} /><KV label="4H setup" value={analysis.strategy?.setup_4h || 'None'} /><KV label="1H trigger" value={analysis.strategy?.trigger_1h || 'None'} /><KV label="Strategy score" value={`${analysis.strategy?.score ?? '—'} / 100`} /></div></section><section className="panel"><PanelTitle title="Trade geometry" subtitle="Calculated from strategy structure" /><div className="signal-levels vertical"><Level label="ENTRY" value={money(analysis.strategy?.entry)} /><Level label="STOP LOSS" value={money(analysis.strategy?.stop)} /><Level label="TAKE PROFIT 1" value={money(analysis.strategy?.tp1)} /><Level label="TAKE PROFIT 2" value={money(analysis.strategy?.tp2)} /></div>{analysis.risk_plan && <div className="kv-list"><KV label="Risk budget" value={`$${money(analysis.risk_plan.risk_budget_usdt)}`} /><KV label="Contracts" value={money(analysis.risk_plan.contracts)} /><KV label="Notional" value={`$${money(analysis.risk_plan.notional_usdt)}`} /><KV label="Est. initial margin" value={`$${money(analysis.risk_plan.initial_margin_estimate_usdt)}`} /></div>}</section></div><div className="content-grid"><section className="panel"><PanelTitle title="Kronos path ensemble" subtitle="Uncalibrated path-share statistics" /><KV label="Bullish path share" value={pct(analysis.forecast?.summary?.bullish_path_share)} /><KV label="Bearish path share" value={pct(analysis.forecast?.summary?.bearish_path_share)} /><KV label="P50 final return" value={pct(analysis.forecast?.summary?.return_quantiles?.p50)} /><KV label="Calibration" value={analysis.forecast?.summary?.calibration_status || 'Unavailable'} /></section><section className="panel"><PanelTitle title="Simulation results" subtitle="Kronos paths and historical blocks remain separate" /><KV label="Kronos TP2 reached" value={analysis.kronos_tp_sl_race?.tp2_reached_before_sl ?? '—'} /><KV label="Kronos SL first" value={analysis.kronos_tp_sl_race?.sl_first ?? '—'} /><KV label="Historical bootstrap mean net R" value={money(analysis.historical_scenario_race?.mean_net_r)} /><KV label="Historical 10,000 replays" value={analysis.historical_scenario_race?.paths ?? '—'} /></section></div></> : <section className="panel empty-panel"><div className="empty-orb">◈</div><h3>Run a complete symbol analysis</h3><p>We will fetch MEXC data, evaluate four timeframes, generate 1,000 Kronos paths, replay 10,000 historical scenarios and calculate paper risk.</p><button className="primary-button" onClick={() => action('/api/analyze', { symbol }, 'Symbol analysis')}>Start analysis</button></section>}</div>}

      {tab === 'backtest' && <div className="page-stack"><div className="page-intro"><div><div className="eyebrow">CHRONOLOGICAL · EVENT-DRIVEN · COST-AWARE</div><h1>Historical backtesting</h1><p>Signal at a completed 1H close; simulated entry no earlier than the next candle open.</p></div><div className="button-pair"><select value={days} onChange={e => setDays(Number(e.target.value))}><option value={7}>7 days</option><option value={30}>30 days</option><option value={90}>90 days</option><option value={180}>180 days</option></select><button className="primary-button" onClick={() => action('/api/backtests', { symbol, days, use_kronos_filter: useKronosFilter }, 'Backtest')}>▶ Run backtest</button></div></div>
        <section className="panel"><div className="control-row"><label className="check-label"><input type="checkbox" checked={useKronosFilter} onChange={e => setUseKronosFilter(e.target.checked)} /> Apply Kronos path-share filter (computationally expensive; up to 128 samples per historical candidate)</label><span className="muted-tag">NO LIVE ORDERS</span></div>{selectedBacktest && <><div className="metric-grid compact-grid"><Metric label="TRADES" value={String(metrics?.trades ?? 0)} /><Metric label="WIN RATE" value={pct(metrics?.win_rate)} /><Metric label="NET P&L" value={`$${money(metrics?.net_profit_usdt)}`} accent={(metrics?.net_profit_usdt || 0) >= 0 ? 'green' : 'red'} /><Metric label="MAX DRAWDOWN" value={`${money(metrics?.max_drawdown_pct)}%`} accent="red" /><Metric label="PROFIT FACTOR" value={money(metrics?.profit_factor)} /><Metric label="EXPECTANCY" value={`${money(metrics?.expectancy_r)}R`} /></div><div className="report-header"><h3>{selectedBacktest.symbol} · {selectedBacktest.metrics?.trades} trades</h3><span className="muted-tag">{selectedBacktest.metrics?.funding_data_status}</span></div><div className="kv-list"><KV label="Gross profit" value={`$${money(metrics?.gross_profit_usdt)}`} /><KV label="Fees" value={`$${money(metrics?.fees_usdt)}`} /><KV label="Slippage" value={`$${money(metrics?.slippage_usdt)}`} /><KV label="Funding" value={metrics?.funding_usdt == null ? 'Not available (not assumed zero)' : `$${money(metrics?.funding_usdt)}`} /></div><div className="table-wrap"><table><thead><tr><th>ENTRY TIME</th><th>SIDE</th><th>ENTRY</th><th>EXIT</th><th>OUTCOME</th><th>NET R</th><th>NET P&L</th></tr></thead><tbody>{(selectedBacktest.trades || []).slice(0, 100).map((t: any, i: number) => <tr key={i}><td>{new Date(t.entry_time).toLocaleString()}</td><td><span className={`status-pill ${t.side === 'LONG' ? 'positive' : 'negative'}`}>{t.side}</span></td><td>{money(t.entry)}</td><td>{money(t.exit_price)}</td><td>{t.exit_type}</td><td className={t.net_r >= 0 ? 'green-text' : 'red-text'}>{money(t.net_r)}R</td><td>${money(t.net_pnl_usdt)}</td></tr>)}</tbody></table></div>{selectedBacktest.warnings?.length > 0 && <div className="warning-box">{selectedBacktest.warnings.join(' · ')}</div>}</>}{!selectedBacktest && <Empty text="Choose a period and run a backtest. Reports include costs, funding availability, equity curve and unresolved-position handling." />}</section><section className="panel"><PanelTitle title="Saved backtest reports" subtitle="Load previous research results" /><div className="list-rows">{backtests.map(b => <button className="list-row" key={b.id} onClick={() => loadReport(b.id)}><div><b>{b.symbol}</b><small>{new Date(b.start_time).toLocaleDateString()} — {new Date(b.end_time).toLocaleDateString()}</small></div><div className="list-row-right"><span>{b.metrics?.trades ?? 0} trades</span><b>{money(b.metrics?.expectancy_r)}R expectancy</b></div></button>)}{!backtests.length && <Empty text="No completed backtests yet." />}</div></section></div>}

      {tab === 'signals' && <div className="page-stack"><div className="page-intro"><div><div className="eyebrow">RESEARCH CANDIDATES · PAPER MODE</div><h1>Signal feed</h1><p>Only multi-timeframe candidates that pass the research engine are stored. No exchange orders are placed.</p></div><button className="primary-button" onClick={() => action('/api/scan', { confirm: true }, 'Market scan')}>⌕ Scan markets</button></div><section className="panel"><SignalTable rows={signals} /></section><section className="panel policy-panel"><div className="policy-icon">!</div><div><b>WhatsApp signal delivery is disabled</b><p>Meta's current Business Messaging Policy restricts promoting or facilitating exchange of real or virtual currency. Use this authenticated dashboard's signal feed instead.</p></div><span className="status-pill negative">POLICY BLOCKED</span></section></div>}

      {tab === 'risk' && <div className="page-stack"><div className="page-intro"><div><div className="eyebrow">POSITION SIZE · MARGIN · MAX LOSS</div><h1>Risk desk</h1><p>Risk calculations are estimates for paper research. They do not model the full MEXC cross-margin liquidation engine.</p></div></div><div className="metric-grid"><Metric label="PAPER EQUITY" value={`$${money(status?.default_account_equity || 1000)}`} sub="Configurable in .env" /><Metric label="RISK PER TRADE" value="0.50%" sub="Default research setting" /><Metric label="DEFAULT LEVERAGE" value="2×" sub="Sizing assumption only" /><Metric label="LIVE TRADING" value="DISABLED" sub="No order endpoints" accent="green" /></div><section className="panel"><PanelTitle title="Risk principles" subtitle="Hard controls applied before a paper signal is approved" /><div className="risk-principles"><div><b>01</b><strong>Stop-defined risk</strong><p>Position size is based on entry-to-stop distance, contract multiplier, fees and slippage.</p></div><div><b>02</b><strong>Portfolio cap</strong><p>Exposure and aggregate planned stop-risk must remain within configured limits.</p></div><div><b>03</b><strong>Liquidation is separate</strong><p>Maintenance margin and liquidation estimates are approximate, not exchange-exact.</p></div><div><b>04</b><strong>No private API keys</strong><p>The initial application uses public market data only and contains no order-placement function.</p></div></div></section></div>}

      {tab === 'settings' && <div className="page-stack"><div className="page-intro"><div><div className="eyebrow">CONFIGURATION · MODEL · DATA · SAFETY</div><h1>System settings</h1><p>Operational defaults are controlled through the deployment environment, not client-side secrets.</p></div></div><section className="panel"><PanelTitle title="Model configuration" subtitle="Pinned revisions and forecast settings" /><KV label="Kronos code revision" value={status?.kronos_source_revision?.slice(0, 12)} /><KV label="Model" value={status?.kronos_model_id} /><KV label="Model revision" value={status?.kronos_model_revision?.slice(0, 12)} /><KV label="Forecast samples" value={status?.forecast_samples} /><KV label="Horizon" value={`${status?.forecast_horizon || 24} × 1H bars`} /><KV label="MEXC REST" value={status?.rest_base_url} /></section><section className="panel"><PanelTitle title="Notification policy" subtitle="No bypasses or unofficial automation" /><div className="policy-panel"><div className="policy-icon">!</div><div><b>WhatsApp crypto-futures alerts are blocked</b><p>The adapter refuses to send trading signals. In-app dashboard alerts and signal history remain available.</p></div><span className="status-pill negative">DISABLED</span></div></section></div>}

      <div className="bottom-status"><span><i className="pulse-dot" /> SYSTEM {status ? 'HEALTHY' : 'CONNECTING'}</span><span>RESEARCH MODE</span><span>LAST REFRESH {new Date().toLocaleTimeString()}</span><span>NO LIVE EXECUTION</span></div>
    </main>
  </div>
}

function compact(value?: number) { if (value == null) return '—'; const n = Number(value); if (n >= 1e9) return `${(n/1e9).toFixed(2)}B`; if (n >= 1e6) return `${(n/1e6).toFixed(2)}M`; if (n >= 1e3) return `${(n/1e3).toFixed(2)}K`; return money(n) }
function Metric({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent?: string }) { return <div className="metric-card"><small>{label}</small><b className={accent === 'green' ? 'green-text' : accent === 'red' ? 'red-text' : ''}>{value}</b><span>{sub}</span></div> }
function PanelTitle({ title, subtitle, action }: { title: string; subtitle?: string; action?: React.ReactNode }) { return <div className="panel-title"><div><h3>{title}</h3>{subtitle && <small>{subtitle}</small>}</div>{action}</div> }
function Empty({ text }: { text: string }) { return <div className="empty-state"><div>◇</div><p>{text}</p></div> }
function Level({ label, value }: { label: string; value: string }) { return <div className="level"><small>{label}</small><b>{value}</b></div> }
function KV({ label, value }: { label: string; value: any }) { return <div className="kv-row"><span>{label}</span><b>{value == null ? '—' : String(value)}</b></div> }
function HealthRows({ status }: { status: any }) { return <div className="health-rows"><KV label="MEXC REST domain" value={status?.rest_base_url || '—'} /><KV label="Kronos compute worker" value={status?.compute_worker_online ? 'Online · model loads on first inference' : 'Offline / not reporting'} /><KV label="Model" value={status?.kronos_model_id || '—'} /><KV label="Auto signal scan" value="Enabled by worker setting" /><KV label="WhatsApp" value="Blocked by policy" /><KV label="Live execution" value="Disabled" /></div> }
function MarketTable({ rows, onSelect, large }: { rows: any[]; onSelect: (s: string) => void; large?: boolean }) { return <div className="table-wrap"><table><thead><tr><th>CONTRACT</th><th>LAST PRICE</th><th>24H TURNOVER</th><th>OPEN INTEREST</th><th>SPREAD</th><th>FUNDING</th><th /></tr></thead><tbody>{rows.map(r => <tr key={r.symbol} onClick={() => onSelect(r.symbol)} className="click-row"><td><b>{r.symbol}</b><small>{r.contract?.baseCoin || ''} perpetual</small></td><td>${money(r.last_price ?? r.ticker?.lastPrice)}</td><td>${compact(r.turnover_24h ?? r.ticker?.amount24)}</td><td>{compact(r.open_interest_contracts ?? r.ticker?.holdVol)}</td><td>{r.spread_bps == null ? '—' : `${Number(r.spread_bps).toFixed(2)} bps`}</td><td>{r.funding_rate == null ? '—' : `${(Number(r.funding_rate)*100).toFixed(4)}%`}</td><td><span className="row-arrow">↗</span></td></tr>)}{!rows.length && <tr><td colSpan={7}>No market data loaded. Run a scan or refresh.</td></tr>}</tbody></table></div> }
function SignalTable({ rows }: { rows: any[] }) { return <div className="table-wrap"><table><thead><tr><th>SYMBOL</th><th>DIRECTION</th><th>ENTRY</th><th>STOP</th><th>TP1</th><th>TP2</th><th>SCORE</th><th>STATUS</th><th>TIME</th></tr></thead><tbody>{rows.map(r => <tr key={r.id}><td><b>{r.symbol}</b></td><td><span className={`status-pill ${r.side === 'LONG' ? 'positive' : 'negative'}`}>{r.side}</span></td><td>{money(r.entry)}</td><td>{money(r.stop)}</td><td>{money(r.tp1)}</td><td>{money(r.tp2)}</td><td>{r.score}/100</td><td><span className={`status-pill ${statusClass(r.status)}`}>{r.status}</span></td><td>{new Date(r.created_at).toLocaleString()}</td></tr>)}{!rows.length && <tr><td colSpan={9}>No approved paper signals. Candidate rejection reasons are available in completed analysis jobs.</td></tr>}</tbody></table></div> }
function JobTable({ jobs }: { jobs: Job[] }) { return <div className="list-rows">{jobs.map(j => <div className="list-row" key={j.id}><div><b>{j.id.slice(0, 10)} · {j.type}</b><small>{j.progress_message}</small></div><div className="list-row-right"><span>{j.progress.toFixed(0)}%</span><b className={`status-pill ${statusClass(j.status)}`}>{j.status}</b></div></div>)}{!jobs.length && <Empty text="No scan jobs yet." />}</div> }
