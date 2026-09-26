import { NextRequest, NextResponse } from "next/server";
import { getSummary, resolveDate } from "@/lib/queries";

export const dynamic = "force-dynamic";

export function GET(req: NextRequest) {
  const date = resolveDate(req.nextUrl.searchParams.get("date"));
  if (!date) return NextResponse.json({ error: "尚無資料，請先執行 scripts/backfill.py" }, { status: 404 });
  return NextResponse.json(getSummary(date));
}
