'''
Small shared helper for Google Classroom API list calls.

Classroom list methods return results in pages (students().list only gives 30 per page by
default), so a single .execute() can silently miss data. list_all() follows nextPageToken
until everything has been fetched.

Usage:
    from classroom_api import list_all
    work = list_all(service.courses().courseWork().list, 'courseWork', courseId=course_id)
'''


# Classroom sometimes answers with a temporary "HTTP 500 Internal error" (or 429/503) on a random
# course. num_retries makes the Google client wait and try again (exponential backoff, up to
# about a minute in total) instead of stopping the whole run. Only use it for read calls.
NUM_RETRIES = 6


def list_all(list_method, key, **kwargs):
    items = []
    page_token = None
    while True:
        res = list_method(pageToken=page_token, **kwargs).execute(num_retries=NUM_RETRIES)
        items.extend(res.get(key, []))
        page_token = res.get('nextPageToken')
        if not page_token:
            return items
