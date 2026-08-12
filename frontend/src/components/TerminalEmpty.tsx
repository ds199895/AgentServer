import { Button } from '@/components/ui/button'

type Props = {
  missingTerminalId: string | null
  hasSessions: boolean
  onBack: () => void
}

export function TerminalEmpty({ missingTerminalId, hasSessions, onBack }: Props) {
  return (
    <section className="flex h-full flex-col items-center justify-center bg-[#090d12] bg-[radial-gradient(circle_at_50%_44%,#17392c44,transparent_28%)] p-[30px] text-center">
      <div className="mb-5 flex items-center gap-2 font-mono text-xl text-primary">
        &gt;_
        <i className="h-[22px] w-2.5 animate-blink bg-primary" />
      </div>
      <h1 className="mt-0 mb-2.5 text-[21px] text-[#e2eaf0]">
        {missingTerminalId ? '终端会话不存在' : hasSessions ? '选择一个终端' : '还没有打开终端'}
      </h1>
      <p className="mt-0 mb-[22px] max-w-[520px] text-xs leading-[1.7] text-[#697783]">
        {missingTerminalId ? (
          <>UUID 为 <code className="font-mono text-[10px] wrap-anywhere text-[#95a9b9]">{missingTerminalId}</code> 的会话可能已被关闭，或 AgentServer 服务已经重启。</>
        ) : hasSessions ? (
          '从上方终端标签中选择一个会话。切换页面或刷新浏览器不会关闭终端。'
        ) : (
          '请从设备列表选择一台 SSH 可用的设备并打开终端。'
        )}
      </p>
      <Button variant="outline" onClick={onBack}>返回设备列表</Button>
    </section>
  )
}
