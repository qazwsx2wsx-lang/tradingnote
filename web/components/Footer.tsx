export default function Footer() {
  return (
    <footer className="muted space-y-3 px-1 pb-10 pt-2 text-xs leading-relaxed">
      <div>
        <h3 className="mb-1 font-semibold text-[var(--text)]">計算方式</h3>
        <ul className="list-disc space-y-0.5 pl-5">
          <li>盤後結論金額：直接採用證交所與櫃買中心公布的三大法人買賣差額（含 ETF）。</li>
          <li>個股買賣超金額（億）＝ 買賣超張數 × 1000 × 當日收盤價 ÷ 1 億；外資含外資自營商。</li>
          <li>類股買賣超＝成分股金額加總（上市＋上櫃官方產業別，排除 ETF、權證）。</li>
          <li>近 5 日、近 20 日：以交易日計算的累計值。</li>
          <li>加速流入（億/天）＝ 近 5 日平均每日買超 − 前 5 日（第 6～10 日）平均每日買超。</li>
          <li>連續買賣：連續買超（正）或賣超（負）的交易日數。類股近 5 日漲跌：成分股漲跌幅等權平均。</li>
        </ul>
      </div>
      <div>
        <h3 className="mb-1 font-semibold text-[var(--text)]">資料來源</h3>
        <p>臺灣證券交易所、證券櫃檯買賣中心公開資訊。</p>
      </div>
      <p className="font-medium">本網站僅彙整統計公開市場資訊，不構成任何投資建議。</p>
    </footer>
  );
}
