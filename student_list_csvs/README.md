# Student list CSVs

Put each semester's student list exports here (this folder's CSV files are git-ignored: they
contain student data). Import them with:

```
.venv\Scripts\python.exe import_student_lists.py --hs "student_list_csvs\2026 Fall HS Student List.csv" --jh "student_list_csvs\2026 Fall JH Student List.csv" --dry-run
```

The semester is read from the file name (`2026 Fall`, `2027 Spring`), or pass `--semester "2026 Fall"`.

## Columns

Both lists (header row, one student per row):

| Column | Example | Notes |
|---|---|---|
| `Name` | `G10 Jordan Lee (李小明)` | as on your list; the "G10 " prefix and bracketed text are ignored when matching |
| `E Name` | `Jordan Lee` | English name (first names, then last name) |
| `C Name` | `李小明` | optional second-language name |
| `Grade` | `10` | grade level |
| `Transferred` | `TRUE` / blank | `TRUE` rows are kept in the raw copy only |
| `Student email` | `jordan.lee@yourschool.edu` | the student's Classroom account |
| `Parent email` | `parent1@example.com, parent2@example.com` | one or more, separated by commas |
| `Home Room` | `10A` | |
| `Gender` | | optional |

High School list only (optional): `No.`, `From JH` (`V` if the student came from your Junior High).
Junior High list only (optional): `Passport Name`, `Math`, and an English class column named like
`26F English`.

Any other High School column is imported too if you first add a column with the same name to the
`Student_Name_List_HS` table.
