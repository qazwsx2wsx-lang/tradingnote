import { NextResponse } from "next/server";
import { availableDates } from "@/lib/queries";

export const dynamic = "force-dynamic";

export function GET() {
  return NextResponse.json({ dates: availableDates() });
}
