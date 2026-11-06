import pandas as pd
from sqlalchemy import text
import sys

from db import get_db_engine, get_db_path

def main():
    try:
        engine = get_db_engine()
        print(f"Opening {get_db_path()}...")

        with engine.connect() as conn:
            # List tables
            print("\n--- Available Tables ---")
            result = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))
            tables = [row[0] for row in result]

            if not tables:
                print("No tables found.")
                return

            for i, t in enumerate(tables):
                print(f"{i+1}. {t}")

            # Interactive mode check
            if len(sys.argv) > 1:
                # If argument provided, search for that table
                target = sys.argv[1]
                if target in tables:
                    table_name = target
                else:
                    print(f"Table '{target}' not found.")
                    return
            else:
                print("\nEnter table number or name to view (or 'q' to quit):")
                choice = input("> ").strip()
                if choice.lower() == 'q':
                    return

                if choice.isdigit():
                    idx = int(choice) - 1
                    if 0 <= idx < len(tables):
                        table_name = tables[idx]
                    else:
                        print("Invalid number.")
                        return
                else:
                    if choice in tables:
                        table_name = choice
                    else:
                        print("Table not found.")
                        return

            print(f"\n--- First 50 rows of '{table_name}' ---")
            df = pd.read_sql(f'SELECT * FROM "{table_name}" LIMIT 50', conn)

            # Adjust pandas display options for better CLI viewing
            pd.set_option('display.max_columns', None)
            pd.set_option('display.width', 1000)

            if df.empty:
                print("(Table is empty)")
            else:
                print(df.to_string(index=False))
                print(f"\n(Showing up to 50 rows. Total rows: {len(df)})")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
