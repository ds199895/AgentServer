import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Download,
  FileQuestion,
  FileText,
  ImageIcon,
  LoaderCircle,
  Maximize2,
  Minimize2,
  RefreshCw,
  XIcon,
} from 'lucide-react'

import { api, type FileGrant } from '@/api'

const MAX_TEXT_BYTES = 256 * 1024
const MAX_INLINE_BYTES = 64 * 1024 * 1024

type PreviewKind = 'image' | 'text' | 'pdf' | 'download'

type Props = {
  grant: FileGrant | null
  resolving?: boolean
  resolveError?: string
  onClose: () => void
  onRetry?: () => void
  onResolveDownload?: (grant: FileGrant) => Promise<FileGrant>
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '大小未知'
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = value
  let unit = -1
  do {
    size /= 1024
    unit += 1
  } while (size >= 1024 && unit < units.length - 1)
  return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${units[unit]}`
}

function previewKind(grant: FileGrant): PreviewKind {
  const mediaType = (grant.media_type || '').split(';', 1)[0].trim().toLowerCase()
  const lowerName = grant.name.toLowerCase()
  if (
    mediaType === 'text/html'
    || mediaType === 'application/xhtml+xml'
    || mediaType === 'image/svg+xml'
    || lowerName.endsWith('.html')
    || lowerName.endsWith('.htm')
    || lowerName.endsWith('.svg')
  ) return 'download'
  if (grant.preview_mode === 'image' && mediaType.startsWith('image/')) return 'image'
  if (grant.preview_mode === 'pdf' && mediaType === 'application/pdf') return 'pdf'
  if (grant.preview_mode === 'text') return 'text'
  return 'download'
}

async function responseError(response: Response): Promise<Error> {
  let message = `读取文件失败 (${response.status})`
  try {
    const body = await response.json() as { detail?: string }
    if (body.detail) message = body.detail
  } catch {
    // A binary endpoint may return an empty or non-JSON error body.
  }
  return new Error(message)
}

async function readLimitedText(
  url: string,
  signal: AbortSignal,
  expectedSize: number,
): Promise<{ text: string; truncated: boolean }> {
  const response = await fetch(url, {
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { Range: `bytes=0-${MAX_TEXT_BYTES - 1}` },
    signal,
  })
  if (!response.ok) throw await responseError(response)
  if (!response.body) {
    const value = new Uint8Array(await response.arrayBuffer())
    return {
      text: new TextDecoder('utf-8', { fatal: false }).decode(value.slice(0, MAX_TEXT_BYTES)),
      truncated: expectedSize > MAX_TEXT_BYTES || value.byteLength > MAX_TEXT_BYTES,
    }
  }

  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let received = 0
  let truncated = expectedSize > MAX_TEXT_BYTES
  try {
    while (received <= MAX_TEXT_BYTES) {
      const { done, value } = await reader.read()
      if (done) break
      if (!value) continue
      const remaining = MAX_TEXT_BYTES - received
      if (value.byteLength > remaining) {
        if (remaining > 0) chunks.push(value.slice(0, remaining))
        received += remaining
        truncated = true
        await reader.cancel()
        break
      }
      chunks.push(value)
      received += value.byteLength
    }
  } finally {
    reader.releaseLock()
  }
  const bytes = new Uint8Array(received)
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.byteLength
  }
  return { text: new TextDecoder('utf-8', { fatal: false }).decode(bytes), truncated }
}

function EmptyPreview() {
  return (
    <div className="grid h-full place-items-center p-8 text-center">
      <div className="max-w-[280px]">
        <FileQuestion className="mx-auto mb-3 size-8 text-[#4f606c]" />
        <strong className="block text-xs text-[#a8b5be]">选择一个文件查看</strong>
        <p className="mt-2 text-[10px] leading-5 text-[#61717d]">
          图片、文本和 PDF 会在这里安全打开；HTML、SVG 与未知格式仅提供下载。
        </p>
      </div>
    </div>
  )
}

export function ArtifactPreview({ grant, resolving = false, resolveError = '', onClose, onRetry, onResolveDownload }: Props) {
  const [objectUrl, setObjectUrl] = useState('')
  const [textContent, setTextContent] = useState('')
  const [truncated, setTruncated] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState('')
  const [actualSize, setActualSize] = useState(false)
  const [contentAttempt, setContentAttempt] = useState(0)
  const kind = useMemo(() => grant ? previewKind(grant) : 'download', [grant])
  const contentUrl = grant ? api.fileContentUrl(grant) : ''

  const downloadFile = async () => {
    if (!grant || downloading) return
    const anchor = document.createElement('a')
    anchor.hidden = true
    anchor.rel = 'noopener'
    anchor.download = grant.name
    document.body.appendChild(anchor)
    setDownloading(true)
    setDownloadError('')
    try {
      let downloadGrant = grant
      if (!grant.immutable) {
        if (!onResolveDownload) throw new Error('无法刷新下载授权，请重新打开文件')
        downloadGrant = await onResolveDownload(grant)
      }
      anchor.href = api.fileContentUrl(downloadGrant)
      anchor.download = downloadGrant.name || grant.name
      anchor.click()
    } catch (reason) {
      setDownloadError(reason instanceof Error ? reason.message : '无法准备文件下载')
    } finally {
      anchor.remove()
      setDownloading(false)
    }
  }

  useEffect(() => {
    setObjectUrl('')
    setTextContent('')
    setTruncated(false)
    setError('')
    setDownloadError('')
    setActualSize(false)
    if (!grant || kind === 'download') {
      setLoading(false)
      return
    }
    if (grant.size > MAX_INLINE_BYTES && kind !== 'text') {
      setError(`文件大于 ${formatBytes(MAX_INLINE_BYTES)}，请下载后查看`)
      setLoading(false)
      return
    }

    const controller = new AbortController()
    let localObjectUrl = ''
    setLoading(true)
    const load = async () => {
      if (kind === 'text') {
        if (grant.size === 0) {
          setTextContent('')
          setTruncated(false)
          return
        }
        const result = await readLimitedText(contentUrl, controller.signal, grant.size)
        if (controller.signal.aborted) return
        setTextContent(result.text)
        setTruncated(result.truncated || grant.size > MAX_TEXT_BYTES)
        return
      }
      const response = await fetch(contentUrl, {
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      })
      if (!response.ok) throw await responseError(response)
      const blob = await response.blob()
      if (controller.signal.aborted) return
      localObjectUrl = URL.createObjectURL(blob)
      setObjectUrl(localObjectUrl)
    }
    void load()
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : '无法读取文件内容')
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })

    return () => {
      controller.abort()
      if (localObjectUrl) URL.revokeObjectURL(localObjectUrl)
    }
  }, [contentAttempt, contentUrl, grant, kind])

  if (resolving) {
    return (
      <div className="grid h-full place-items-center text-xs text-[#82919c]">
        <span className="flex items-center gap-2"><LoaderCircle className="size-4 animate-spin text-primary" />正在准备安全预览…</span>
      </div>
    )
  }

  if (resolveError && !grant) {
    return (
      <div className="grid h-full place-items-center p-6 text-center">
        <div className="max-w-sm rounded-xl border border-[#713640] bg-[#28171b99] p-5">
          <AlertTriangle className="mx-auto mb-3 size-6 text-[#ff8995]" />
          <p className="m-0 text-xs leading-5 text-[#ffadb5]">{resolveError}</p>
          <div className="mt-4 flex justify-center gap-2">
            {onRetry && <button type="button" onClick={onRetry} className="flex h-8 cursor-pointer items-center gap-1.5 rounded-md border border-[#47545f] px-3 text-[10px] text-[#c5d0d8] hover:border-[#668071]"><RefreshCw className="size-3" />重试</button>}
            <button type="button" onClick={onClose} className="h-8 cursor-pointer rounded-md border border-[#47545f] px-3 text-[10px] text-[#c5d0d8]">返回</button>
          </div>
        </div>
      </div>
    )
  }

  if (!grant) return <EmptyPreview />

  return (
    <section className="grid h-full min-h-0 grid-rows-[48px_minmax(0,1fr)] bg-[#070b0f]">
      <header className="flex min-w-0 items-center gap-2 border-b border-[#26323c] bg-[#0d141a] px-3">
        {kind === 'image' ? <ImageIcon className="size-4 flex-none text-primary" /> : <FileText className="size-4 flex-none text-[#82a6bd]" />}
        <div className="min-w-0 flex-1">
          <strong className="block truncate text-xs text-[#e2ebf1]" title={grant.name}>{grant.name}</strong>
          <small className="block truncate font-mono text-[8px] text-[#657581]" title={grant.path}>
            {grant.image_width && grant.image_height ? `${grant.image_width}×${grant.image_height} · ` : ''}{formatBytes(grant.size)} · {grant.media_type || '未知格式'}{grant.immutable ? ' · 不可变附件' : ''}
          </small>
        </div>
        {kind === 'image' && objectUrl && (
          <button type="button" onClick={() => setActualSize((value) => !value)} aria-label={actualSize ? '适应窗口' : '按原始尺寸显示'} title={actualSize ? '适应窗口' : '1:1 原图'} className="grid size-8 cursor-pointer place-items-center rounded-md border border-[#34404b] bg-[#121a21] text-[#9cabb6] hover:text-primary">
            {actualSize ? <Minimize2 className="size-3.5" /> : <Maximize2 className="size-3.5" />}
          </button>
        )}
        <button type="button" onClick={() => void downloadFile()} disabled={downloading} className="grid size-8 cursor-pointer place-items-center rounded-md border border-[#34404b] bg-[#121a21] text-[#9cabb6] hover:text-primary disabled:cursor-wait disabled:opacity-60" aria-label={downloading ? `正在下载 ${grant.name}` : `下载 ${grant.name}`} title={downloading ? '正在刷新下载授权…' : '下载原文件'}>{downloading ? <LoaderCircle className="size-3.5 animate-spin" /> : <Download className="size-3.5" />}</button>
        <button type="button" onClick={onClose} className="grid size-8 cursor-pointer place-items-center rounded-md border border-[#34404b] bg-[#121a21] text-[#9cabb6] hover:text-white max-md:flex" aria-label="关闭文件预览" title="返回文件列表"><XIcon className="size-3.5" /></button>
      </header>
      <div className="relative min-h-0 overflow-auto">
        {downloadError && (
          <div role="alert" className="absolute top-2 right-2 z-30 flex max-w-[min(360px,calc(100%-16px))] items-center gap-2 rounded-lg border border-[#713640] bg-[#28171bf2] px-3 py-2 text-[10px] text-[#ffadb5] shadow-xl">
            <AlertTriangle className="size-3.5 flex-none" />
            <span className="min-w-0 flex-1">{downloadError}</span>
            <button type="button" onClick={() => setDownloadError('')} className="grid size-5 cursor-pointer place-items-center rounded text-[#d58c94] hover:bg-[#47252b] hover:text-white" aria-label="关闭下载错误"><XIcon className="size-3" /></button>
          </div>
        )}
        {loading && (
          <div className="absolute inset-0 z-10 grid place-items-center bg-[#070b0fe8] text-xs text-[#82919c]">
            <span className="flex items-center gap-2"><LoaderCircle className="size-4 animate-spin text-primary" />正在读取 {grant.name}…</span>
          </div>
        )}
        {error && (
          <div className="grid h-full place-items-center p-6 text-center">
            <div className="max-w-sm">
              <AlertTriangle className="mx-auto mb-3 size-6 text-[#ff8995]" />
              <p className="m-0 text-xs leading-5 text-[#ffadb5]">{error}</p>
              <div className="mt-4 flex justify-center gap-2">
                <button type="button" onClick={() => onRetry ? onRetry() : setContentAttempt((value) => value + 1)} className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-md border border-[#47545f] px-3 text-[10px] text-[#c5d0d8] hover:border-[#668071]"><RefreshCw className="size-3" />{onRetry ? '重新授权' : '重试'}</button>
                <button type="button" onClick={() => void downloadFile()} disabled={downloading} className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-md border border-[#47545f] px-3 text-[10px] text-[#c5d0d8] hover:border-[#668071] disabled:cursor-wait disabled:opacity-60">{downloading ? <LoaderCircle className="size-3 animate-spin" /> : <Download className="size-3" />}{downloading ? '准备下载…' : '下载文件'}</button>
              </div>
            </div>
          </div>
        )}
        {!error && kind === 'image' && objectUrl && (
          <div className="flex min-h-full min-w-full items-center justify-center bg-[linear-gradient(45deg,#0b1117_25%,transparent_25%),linear-gradient(-45deg,#0b1117_25%,transparent_25%),linear-gradient(45deg,transparent_75%,#0b1117_75%),linear-gradient(-45deg,transparent_75%,#0b1117_75%)] bg-[length:20px_20px] bg-[position:0_0,0_10px,10px_-10px,-10px_0px] p-4">
            <img src={objectUrl} alt={grant.name} className={actualSize ? 'max-w-none shrink-0' : 'max-h-full max-w-full object-contain'} />
          </div>
        )}
        {!error && kind === 'text' && !loading && (
          <div className="min-h-full bg-[#090e13]">
            {truncated && <div className="sticky top-0 z-10 border-b border-[#5f4d29] bg-[#2a2315ee] px-3 py-2 text-[10px] text-[#f1c974]">仅显示前 {formatBytes(MAX_TEXT_BYTES)}；下载可查看完整文件。</div>}
            <pre className="m-0 min-w-full whitespace-pre-wrap break-words p-4 font-mono text-[11px] leading-5 text-[#d3dde5] selection:bg-[#315a48]">{textContent || '（空文件）'}</pre>
          </div>
        )}
        {!error && kind === 'pdf' && objectUrl && (
          <iframe src={objectUrl} title={`${grant.name} PDF 预览`} sandbox="" referrerPolicy="no-referrer" className="h-full min-h-[420px] w-full border-0 bg-white" />
        )}
        {!error && kind === 'download' && (
          <div className="grid h-full place-items-center p-7 text-center">
            <div className="max-w-sm">
              <FileQuestion className="mx-auto mb-3 size-8 text-[#647581]" />
              <strong className="block text-sm text-[#dce6ed]">此格式不在网页中打开</strong>
              <p className="mt-2 text-[10px] leading-5 text-[#71818c]">为避免执行不受信任内容，HTML、SVG 和未知格式只允许下载原文件。</p>
              <button type="button" onClick={() => void downloadFile()} disabled={downloading} className="mt-4 inline-flex h-9 cursor-pointer items-center gap-2 rounded-md border-0 bg-primary px-4 text-[11px] font-bold text-[#07120d] hover:bg-[#9dffd0] disabled:cursor-wait disabled:opacity-60">{downloading ? <LoaderCircle className="size-3.5 animate-spin" /> : <Download className="size-3.5" />}{downloading ? '准备下载…' : `下载 ${grant.name}`}</button>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}
