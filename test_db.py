def fetch_links_from_database(integration_id):
    articles = []
    try:
        conn = psycopg2.connect(
            dbname=settings.POSTGRES_DATABASE,
            user=settings.POSTGRES_USERNAME,
            password=settings.POSTGRES_PASSWORD,
            host=settings.POSTGRES_HOSTNAME,
        )
        print(f"Connected: {conn}")

        c2 = conn.cursor()
        c2.execute("SELECT source_id, COUNT(*) FROM urls GROUP BY source_id")
        rows = c2.fetchall()
        if rows:
            print("URLs in database by source_id:")
            for r in rows:
                print(f"  {r[0]}: {r[1]}")
        else:
            print("URLs table is completely empty")
        c2.close()

        cursor = conn.cursor("urls")
        sql_query = f"SELECT link, source_id, created_at, data_id FROM urls WHERE source_id = '{integration_id}'"
        cursor.execute(sql_query)
        return cursor

    except psycopg2.Error as e:
        print("Error connecting to PostgreSQL:", e)
    return articles
