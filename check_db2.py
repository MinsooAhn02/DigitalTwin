import sqlite3
con = sqlite3.connect('node-link-data/nodelink.sqlite')
cur = con.cursor()
cur.execute("PRAGMA table_info(nodes)")
print('nodes columns:')
for c in cur.fetchall():
    print(' ', c)
cur.execute("SELECT COUNT(*) FROM nodes")
print('nodes count:', cur.fetchone())
# Sample link
cur.execute("SELECT * FROM links LIMIT 1")
print('sample link:', cur.fetchone())
con.close()
