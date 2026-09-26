import type { ReactNode } from "react";

export default function Section({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="card p-4 sm:p-5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold sm:text-lg">{title}</h2>
          {subtitle && <p className="muted mt-0.5 text-xs sm:text-sm">{subtitle}</p>}
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}
