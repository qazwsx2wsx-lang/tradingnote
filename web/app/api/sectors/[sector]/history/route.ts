import { NextRequest, NextResponse } from "next/server";
import { getSectorHistory, isInvestor, resolveDate } from "@/lib/queries";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest, ctx: { params: Promise<{ sector: string }> }) {
  const { sector } = await ctx.params;
  const params = req.nextUrl.searchParams;
  const investor = params.get("investor") ?? "all";
  if (!isInvestor(investor)) return NextResponse.json({ error: "investor 無效" }, { status: 400 });
  const days = Math.min(Math.max(Number(params.get("days") ?? 30) || 30, 1), 250);
  const date = resolveDate(params.get("date"));
  if (!date) return NextResponse.json({ error: "尚無資料" }, { status: 404 });
  const name = decodeURIComponent(sector);
  return NextResponse.json({ sector: name, investor, history: getSectorHistory(name, investor, days, date) });
}
