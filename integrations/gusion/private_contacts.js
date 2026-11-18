// SPDX-License-Identifier: GPL-3.0-or-later
function contactQueryPeople(dbPath, query, exact) {
    const db = SqliteDatabase.open(dbPath, { flags: ["readonly"] });
    try {
        const display = "COALESCE(NULLIF(TRIM(remark), ''), NULLIF(TRIM(nick_name), ''), NULLIF(TRIM(alias), ''), username)";
        const base = "SELECT username, substr(" + display + ", 1, 200) FROM contact " +
            "WHERE COALESCE(delete_flag, 0) = 0 AND length(username) BETWEEN 1 AND 128 " +
            "AND username NOT GLOB '*[^A-Za-z0-9_-]*' AND substr(lower(username), 1, 3) <> 'gh_' ";
        const sql = exact ? base + "AND username = ?1 COLLATE BINARY LIMIT 2" :
            base + "AND (instr(lower(username), lower(?1)) > 0 OR instr(lower(alias), lower(?1)) > 0 " +
                "OR instr(lower(remark), lower(?1)) > 0 OR instr(lower(nick_name), lower(?1)) > 0) " +
                "ORDER BY (username = ?1) DESC, (" + display + " = ?1) DESC, username LIMIT 20";
        const statement = db.prepare(sql);
        statement.bindText(1, query);
        const contacts = [];
        for (let row = statement.step(); row !== null; row = statement.step()) {
            contacts.push({ user_id: row[0], display_name: row[1] });
        }
        return contacts;
    } finally {
        db.close();
    }
}
