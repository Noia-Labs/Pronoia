import { useEffect, useState } from "react";
import { Loader2, Save } from "lucide-react";
import { api } from "../api";
import type { ModelLabProfile } from "../types";

export default function PlatformBaseModelSettings({ profiles, defaultProfileId, onChanged }: {
  profiles: ModelLabProfile[];
  defaultProfileId: string | null;
  onChanged: () => void;
}) {
  const [selected, setSelected] = useState(defaultProfileId ?? "");
  const [current, setCurrent] = useState("正在读取…");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    setSelected(defaultProfileId ?? "");
    let active = true;
    void api.modelLabEventCapabilities().then((data) => {
      if (active) setCurrent(data.platform_default.available ? `Pronoia（${data.platform_default.model_id ?? data.platform_default.name}）` : "尚未配置平台基模");
    }).catch(() => { if (active) setCurrent("当前配置读取失败，请刷新"); });
    return () => { active = false; };
  }, [defaultProfileId]);
  const save = async () => {
    setSaving(true); setMessage("");
    try {
      await api.modelLabSetDefaultProfile(selected);
      setMessage("已更新平台统一基模。已创建的实验仍使用创建时的配置。");
      onChanged();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSaving(false); }
  };
  return <section className="max-w-3xl rounded-2xl border border-edge bg-card p-6 sm:p-8">
    <p className="text-[10px] font-semibold uppercase tracking-[.2em] text-violet">PLATFORM BASE MODEL</p>
    <h2 className="mt-2 font-serif text-2xl text-ink">平台统一基模</h2>
    <p className="mt-3 text-sm leading-relaxed text-mute">这里设置研究工作台和事件测试中 Pronoia 共用的基模。基模评测会分别使用你选中的连接运行同一套多 Agent 流程，评测选择不会修改这里。</p>
    <div className="mt-6 rounded-xl bg-paper p-4"><div className="text-xs text-faint">当前平台基模</div><div className="mt-2 break-words text-sm font-medium text-ink">{current}</div></div>
    <label className="mt-6 block text-sm font-medium text-ink">选择已保存的连接
      <select value={selected} onChange={(event) => { setSelected(event.target.value); setMessage(""); }} className="mt-2 w-full rounded-xl border border-edgeDark bg-card px-4 py-3">
        <option value="">请选择平台基模连接</option>
        {profiles.filter((profile) => profile.is_active).map((profile) => <option key={profile.id} value={profile.id} disabled={!profile.secret_configured}>{profile.name} · {profile.model_id}{!profile.secret_configured ? "（请先填写 API Key）" : ""}</option>)}
      </select>
    </label>
    <p className="mt-2 text-xs text-faint">在“基模连接”中添加 API 地址、模型 ID 和 API Key 后，可在这里选择。</p>
    <button type="button" disabled={saving || !selected || selected === defaultProfileId} onClick={() => void save()} className="mt-5 inline-flex items-center gap-2 rounded-xl bg-ink px-5 py-3 text-sm font-semibold text-card disabled:opacity-40">{saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}应用为平台统一基模</button>
    {message && <p role="status" className="mt-4 text-sm text-mute">{message}</p>}
  </section>;
}
