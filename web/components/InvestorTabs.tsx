"use client";

import { INVESTOR_LABEL, INVESTORS, type Investor } from "@/lib/types";

export default function InvestorTabs({
  value,
  onChange,
}: {
  value: Investor;
  onChange: (v: Investor) => void;
}) {
  return (
    <div role="tablist" className="inline-flex rounded-lg border border-[var(--border)] p-0.5 text-sm">
      {INVESTORS.map((inv) => (
        <button
          key={inv}
          role="tab"
          aria-selected={value === inv}
          onClick={() => onChange(inv)}
          className={`rounded-md px-3 py-1 transition-colors ${
            value === inv ? "bg-accent text-white" : "muted hover:text-[var(--text)]"
          }`}
        >
          {INVESTOR_LABEL[inv]}
        </button>
      ))}
    </div>
  );
}
