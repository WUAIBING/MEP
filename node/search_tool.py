import os
import requests

GOOGLE_SEARCH_API_KEY = os.getenv("GOOGLE_SEARCH_API_KEY")
GOOGLE_SEARCH_CX = os.getenv("GOOGLE_SEARCH_CX")

def google_search(query, num_results=5):
    """
    Perform a Google Custom Search.
    Returns a list of result snippets.
    """
    if not query:
        return []
        
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        'key': GOOGLE_SEARCH_API_KEY,
        'cx': GOOGLE_SEARCH_CX,
        'q': query,
        'num': num_results
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        results = []
        if 'items' in data:
            for item in data['items']:
                title = item.get('title', 'No Title')
                snippet = item.get('snippet', 'No Snippet')
                link = item.get('link', '')
                results.append(f"Title: {title}\nLink: {link}\nSnippet: {snippet}\n")
        return results
    except Exception as e:
        print(f"[Search Tool] Error: {e}")
        return []

if __name__ == "__main__":
    # Test
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "OpenClaw AI"
    res = google_search(q)
    print("\n".join(res))
