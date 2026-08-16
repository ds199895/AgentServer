import { useEffect, useRef, useState } from 'react'
import type { Extension } from '@codemirror/state'

type Props = {
  value: string
  filename: string
  lineWrapping: boolean
}

async function languageExtension(filename: string): Promise<Extension[]> {
  const lower = filename.toLowerCase()
  if (/\.(?:js|jsx|mjs|cjs|ts|tsx)$/.test(lower)) {
    const { javascript } = await import('@codemirror/lang-javascript')
    return [javascript({ typescript: /\.(?:ts|tsx)$/.test(lower), jsx: /\.(?:jsx|tsx)$/.test(lower) })]
  }
  if (/\.jsonc?$/.test(lower)) {
    const { json } = await import('@codemirror/lang-json')
    return [json()]
  }
  if (/\.pyw?$/.test(lower)) {
    const { python } = await import('@codemirror/lang-python')
    return [python()]
  }
  if (/\.(?:css|scss|less)$/.test(lower)) {
    const { css } = await import('@codemirror/lang-css')
    return [css()]
  }
  if (/\.(?:html?|svg|xml)$/.test(lower)) {
    const { html } = await import('@codemirror/lang-html')
    return [html()]
  }
  if (/\.(?:md|mdx|markdown)$/.test(lower)) {
    const { markdown } = await import('@codemirror/lang-markdown')
    return [markdown()]
  }
  return []
}

export function ReadonlyCodeViewer({ value, filename, lineWrapping }: Props) {
  const parentRef = useRef<HTMLDivElement>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let disposed = false
    let view: { destroy: () => void } | null = null
    setLoading(true)
    setError('')
    const mount = async () => {
      const [core, stateModule, viewModule, themeModule, language] = await Promise.all([
        import('codemirror'),
        import('@codemirror/state'),
        import('@codemirror/view'),
        import('@codemirror/theme-one-dark'),
        languageExtension(filename),
      ])
      if (disposed || !parentRef.current) return
      const extensions: Extension[] = [
        core.basicSetup,
        themeModule.oneDark,
        stateModule.EditorState.readOnly.of(true),
        viewModule.EditorView.editable.of(false),
        viewModule.EditorView.theme({
          '&': { height: '100%', backgroundColor: '#090e13', fontSize: '11px' },
          '.cm-scroller': {
            overflow: 'auto',
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
          },
          '.cm-content': { padding: '10px 0' },
          '.cm-gutters': { backgroundColor: '#0b1117', borderRight: '1px solid #202c35' },
          '.cm-activeLine': { backgroundColor: '#14201980' },
          '.cm-activeLineGutter': { backgroundColor: '#142019' },
        }),
        ...language,
      ]
      if (lineWrapping) extensions.push(viewModule.EditorView.lineWrapping)
      view = new viewModule.EditorView({
        state: stateModule.EditorState.create({ doc: value, extensions }),
        parent: parentRef.current,
      })
    }
    void mount()
      .catch((reason) => {
        if (!disposed) setError(reason instanceof Error ? reason.message : '代码查看器加载失败')
      })
      .finally(() => {
        if (!disposed) setLoading(false)
      })
    return () => {
      disposed = true
      view?.destroy()
      if (parentRef.current) parentRef.current.replaceChildren()
    }
  }, [filename, lineWrapping, value])

  return (
    <div className="relative h-full min-h-[180px] w-full bg-[#090e13]">
      <div ref={parentRef} className="h-full w-full overflow-hidden" />
      {loading && <div className="absolute inset-0 grid place-items-center bg-[#090e13] text-[10px] text-[#71818c]">正在加载代码查看器…</div>}
      {error && <pre className="absolute inset-0 m-0 overflow-auto whitespace-pre-wrap p-4 font-mono text-[11px] leading-5 text-[#d3dde5]">{value}</pre>}
    </div>
  )
}
