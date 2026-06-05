import sqlite3
con = sqlite3.connect('node-link-data/nodelink.sqlite')
cur = con.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
print('Tables:', cur.fetchall())
cur.execute("PRAGMA table_info(links)")
print('links columns:')
for c in cur.fetchall():
    print(' ', c)
cur.execute("SELECT COUNT(*) FROM links")
print('links count:', cur.fetchone())
con.close()
