import { fmtYi, toneClass } from "@/lib/format";
import { INVESTOR_LABEL, type Investor, type Summary } from "@/lib/types";

const ORDER: Investor[] = ["foreign", "trust", "dealer", "all"];

export default function SummaryCards({ summary }: { summary: Summary | null }) {
  return (
    <section className="card p-4 sm:p-5">
      <h2 className="text-base font-semibold sm:text-lg">盤後結論</h2>
      <p className="mt-1 text-lg font-bold sm:text-xl">{summary ? summary.text : "載入中…"}</p>
      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4 sm:gap-3">
        {ORDER.map((inv) => {
          const v = summary?.amounts[inv];
          return (
            <div key={inv} className="rounded-xl border border-[var(--border)] p-3">
              <div className="muted text-xs">{inv === "all" ? "三大法人合計" : INVESTOR_LABEL[inv]}</div>
              <div className={`num mt-1 text-xl font-bold sm:text-2xl ${toneClass(v)}`}>
                {fmtYi(v)}
                <span className="muted ml-1 text-xs font-normal">億</span>
              </div>
            </div>
          );
        })}
      </div>
      <p className="muted mt-2 text-xs">
        金額為證交所＋櫃買中心公布之三大法人買賣差額（含 ETF）；紅色為買超、綠色為賣超。
      </p>
    </section>
  );
}
