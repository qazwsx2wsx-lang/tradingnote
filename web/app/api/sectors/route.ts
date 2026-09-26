import { NextRequest, NextResponse } from "next/server";
import { getSectors, isInvestor, resolveDate } from "@/lib/queries";

export const dynamic = "force-dynamic";

export function GET(req: NextRequest) {
  const params = req.nextUrl.searchParams;
  const investor = params.get("investor") ?? "all";
  if (!isInvestor(investor)) {
    return NextResponse.json({ error: "investor 必須是 all|foreign|trust|dealer" }, { status: 400 });
  }
  const date = resolveDate(params.get("date"));
  if (!date) return NextResponse.json({ error: "尚無資料" }, { status: 404 });
  return NextResponse.json({ date, investor, sectors: getSectors(date, investor) });
}
