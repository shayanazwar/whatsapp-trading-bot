import { useEffect, useRef } from 'react'

type Candle = { timestamp: string; open: number; high: number; low: number; close: number; volume: number }
type Props = {
  candles: Candle[]
  paths?: number[][][]
  summary?: any
  showWaves: boolean
  showBands: boolean
}

export default function ForecastChart({ candles, paths, summary, showWaves, showBands }: Props) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const box = canvas.getBoundingClientRect()
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    canvas.width = Math.max(1, Math.floor(box.width * dpr))
    canvas.height = Math.max(1, Math.floor(box.height * dpr))
    ctx.scale(dpr, dpr)
    const w = box.width, h = box.height
    ctx.clearRect(0, 0, w, h)
    ctx.fillStyle = '#07111d'; ctx.fillRect(0, 0, w, h)
    const hist = candles.slice(-100)
    if (!hist.length) {
      ctx.fillStyle = '#91a3b8'; ctx.font = '13px Inter, sans-serif'; ctx.fillText('Load market candles to view the chart', 24, 38)
      return
    }
    const allValues: number[] = []
    hist.forEach(c => allValues.push(c.low, c.high))
    const waves = showWaves && paths ? paths : []
    waves.forEach(path => path.forEach(row => allValues.push(row[1], row[2])))
    const q = summary?.close_quantiles_by_step
    if (q) Object.values(q).forEach((series: any) => series.forEach((value: number) => allValues.push(value)))
    const min = Math.min(...allValues) * 0.998
    const max = Math.max(...allValues) * 1.002
    const range = Math.max(max - min, 1e-9)
    const top = 18, bottom = h - 28, plotH = bottom - top
    const y = (v: number) => bottom - ((v - min) / range) * plotH
    const split = w * 0.64
    const histStep = split / hist.length
    const futureWidth = w - split - 10
    const xHist = (i: number) => i * histStep + histStep / 2
    // Horizontal grid and labels.
    ctx.strokeStyle = 'rgba(138,159,184,.12)'; ctx.lineWidth = 1
    ctx.font = '10px Inter, sans-serif'; ctx.fillStyle = '#7890a8'
    for (let i = 0; i <= 4; i++) {
      const yy = top + (plotH * i) / 4
      ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(w, yy); ctx.stroke()
      const price = max - (range * i) / 4
      ctx.fillText(price.toLocaleString(undefined, { maximumFractionDigits: 2 }), 8, yy - 4)
    }
    // Forecast region background and divider.
    ctx.fillStyle = 'rgba(39,224,138,.025)'; ctx.fillRect(split, top, futureWidth, plotH)
    ctx.setLineDash([4, 5]); ctx.strokeStyle = 'rgba(39,224,138,.35)'; ctx.beginPath(); ctx.moveTo(split, top); ctx.lineTo(split, bottom); ctx.stroke(); ctx.setLineDash([])
    // Candlesticks.
    const bodyW = Math.max(2, histStep * 0.58)
    hist.forEach((c, i) => {
      const x = xHist(i)
      const up = c.close >= c.open
      const color = up ? '#29df91' : '#ff657b'
      ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1
      ctx.beginPath(); ctx.moveTo(x, y(c.high)); ctx.lineTo(x, y(c.low)); ctx.stroke()
      const bodyTop = y(Math.max(c.open, c.close)); const bodyBottom = y(Math.min(c.open, c.close))
      ctx.fillRect(x - bodyW / 2, bodyTop, bodyW, Math.max(1.2, bodyBottom - bodyTop))
    })
    const last = hist[hist.length - 1].close
    ctx.strokeStyle = 'rgba(255,255,255,.45)'; ctx.setLineDash([3, 4]); ctx.beginPath(); ctx.moveTo(split, y(last)); ctx.lineTo(w, y(last)); ctx.stroke(); ctx.setLineDash([])
    // Individual Kronos waves: the backend stores all samples; the canvas draws all when enabled.
    if (showWaves && waves.length) {
      waves.forEach(path => {
        if (!path.length) return
        const bullish = path[path.length - 1][3] >= last
        ctx.beginPath(); ctx.lineWidth = 0.75
        ctx.strokeStyle = bullish ? 'rgba(32,224,137,.105)' : 'rgba(255,82,112,.105)'
        path.forEach((row, i) => {
          const xx = split + (i / Math.max(path.length - 1, 1)) * futureWidth
          const yy = y(row[3])
          if (i === 0) ctx.moveTo(xx, yy); else ctx.lineTo(xx, yy)
        })
        ctx.stroke()
      })
    }
    if (showBands && q?.p10 && q?.p50 && q?.p90) {
      const p10 = q.p10 as number[], p50 = q.p50 as number[], p90 = q.p90 as number[]
      ctx.beginPath()
      p90.forEach((v, i) => { const xx = split + (i / Math.max(p90.length - 1, 1)) * futureWidth; if (!i) ctx.moveTo(xx, y(v)); else ctx.lineTo(xx, y(v)) })
      for (let i = p10.length - 1; i >= 0; i--) ctx.lineTo(split + (i / Math.max(p10.length - 1, 1)) * futureWidth, y(p10[i]))
      ctx.closePath(); ctx.fillStyle = 'rgba(73,155,235,.12)'; ctx.fill()
      ctx.beginPath(); p50.forEach((v, i) => { const xx = split + (i / Math.max(p50.length - 1, 1)) * futureWidth; if (!i) ctx.moveTo(xx, y(v)); else ctx.lineTo(xx, y(v)) })
      ctx.strokeStyle = '#d8e5f4'; ctx.lineWidth = 1.6; ctx.stroke()
    }
    ctx.fillStyle = '#a7b8ca'; ctx.font = '10px Inter, sans-serif'
    ctx.fillText('HISTORICAL', 12, h - 8); ctx.fillText(paths?.length ? `KRONOS FUTURE · ${paths.length.toLocaleString()} PATHS` : 'FORECAST NOT LOADED', split + 12, h - 8)
  }, [candles, paths, summary, showWaves, showBands])
  return <canvas className="forecast-canvas" ref={ref} />
}
