import Database from "better-sqlite3";
import path from "node:path";

// 資料由 Python 核心（tradingnote_institutional_history.py）寫入，web 只讀。
const DB_PATH =
  process.env.HISTORY_DB ?? path.resolve(process.cwd(), "..", "data", "history.db");

let db: Database.Database | null = null;

export function getDb(): Database.Database {
  if (!db) {
    db = new Database(DB_PATH, { readonly: true, fileMustExist: true });
  }
  return db;
}
