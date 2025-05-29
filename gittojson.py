import requests
import json
import argparse
from urllib.parse import urlparse
import time

# --- Configuration ---
# Consider adjusting these if you encounter issues with very large repositories or rate limits.
# Delay between API calls to help avoid rate limiting (in seconds)
API_CALL_DELAY = 0.1 

# --- Helper Functions ---

def parse_github_url(repo_url):
    """
    Parses a GitHub repository URL to extract owner and repo name.
    Example: https://github.com/owner/repo -> (owner, repo)
    """
    parsed_url = urlparse(repo_url)
    if parsed_url.hostname != "github.com":
        raise ValueError("Invalid GitHub URL. Must be a github.com URL.")
    
    path_parts = parsed_url.path.strip("/").split("/")
    if len(path_parts) < 2:
        raise ValueError("Invalid GitHub URL format. Expected github.com/owner/repo.")
    
    owner = path_parts[0]
    # Handle cases where repo name might have '.git' suffix from clone URLs
    repo = path_parts[1].replace(".git", "") 
    return owner, repo

def fetch_github_api(api_url, token=None, retries=3, backoff_factor=2):
    """
    Fetches data from the GitHub API with headers for token and retry logic.
    """
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    for attempt in range(retries):
        try:
            # Introduce a small delay before each API call
            time.sleep(API_CALL_DELAY)
            
            response = requests.get(api_url, headers=headers)
            
            # Check for rate limit explicitly
            if response.status_code == 403 and 'X-RateLimit-Remaining' in response.headers and response.headers['X-RateLimit-Remaining'] == '0':
                ratelimit_reset_time = int(response.headers.get('X-RateLimit-Reset', time.time() + 60))
                wait_time = max(0, ratelimit_reset_time - time.time()) + 10 # Add a small buffer
                print(f"Rate limit hit. Waiting for {wait_time:.0f} seconds before retrying...")
                time.sleep(wait_time)
                continue # Retry the current request

            response.raise_for_status()  # Raises an exception for other HTTP errors (4xx, 5xx)
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"API request to {api_url} failed (attempt {attempt + 1}/{retries}): {e}")
            if attempt + 1 == retries: # If it's the last retry
                raise # Re-raise the last exception
            time.sleep(backoff_factor ** attempt) # Exponential backoff
    return None # Should not be reached if retries are exhausted and exception is re-raised

def fetch_repo_contents_recursive(owner, repo, path="", token=None, depth=0, max_depth=10):
    """
    Recursively fetches the contents of a GitHub repository path.
    Includes depth limiting to prevent infinite recursion on symbolic links or very deep structures.
    """
    if depth > max_depth:
        print(f"Warning: Reached maximum recursion depth ({max_depth}) at path '{path}'. Stopping further recursion for this branch.")
        return [{"name": f"MAX_DEPTH_REACHED_at_{path.replace('/','_')}", "type": "error", "path": path, "message": f"Max depth {max_depth} reached"}]

    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    print(f"Fetching (depth {depth}): {api_url}")

    try:
        contents = fetch_github_api(api_url, token)
        if contents is None: # If fetch_github_api failed after retries
             return [{"name": f"FETCH_ERROR_at_{path.replace('/','_')}", "type": "error", "path": path, "message": "Failed to fetch contents after multiple retries"}]
    except requests.exceptions.RequestException as e:
        print(f"Error fetching contents for {owner}/{repo}/{path}: {e}")
        # Return an error object or empty list to allow partial results for the repository
        return [{"name": f"ERROR_fetching_{path.replace('/','_')}", "type": "error", "path": path, "message": str(e)}]


    tree_nodes = []
    if not isinstance(contents, list):
        print(f"Warning: Expected a list of contents for path '{path}', but got {type(contents)}. Content: {contents}")
        # This can happen if the path points to a single file directly,
        # or if it's a submodule (which the contents API describes differently).
        if isinstance(contents, dict) and 'type' in contents:
             # If it's a single file an an unexpected path, treat it as such
            if contents.get("type") == "file":
                 return [{
                    "name": contents["name"],
                    "path": contents["path"],
                    "type": "file",
                    "size": contents.get("size", 0),
                    "url": contents.get("html_url"),
                    "download_url": contents.get("download_url")
                }]
            elif contents.get("type") == "submodule":
                return [{
                    "name": contents["name"],
                    "path": contents["path"],
                    "type": "submodule",
                    "submodule_git_url": contents.get("submodule_git_url"),
                    "url": contents.get("html_url"),
                    "sha": contents.get("sha")
                }]
        return [{"name": f"UNEXPECTED_CONTENT_at_{path.replace('/','_')}", "type": "error", "path": path, "message": "Expected list of items but received different type."}]


    for item in contents:
        # Basic details common to files and directories
        node_details = {
            "name": item["name"],
            "path": item["path"],
            "type": item["type"],
            "sha": item.get("sha"),
            "size": item.get("size", 0), # Size is usually present for files
            "url": item.get("html_url"), # Link to view on GitHub
            "download_url": item.get("download_url") # Present for files
        }

        if item["type"] == "dir":
            # Recursively fetch contents of the subdirectory
            node_details["children"] = fetch_repo_contents_recursive(
                owner, repo, item["path"], token, depth + 1, max_depth
            )
        elif item["type"] == "file":
            # File-specific details are already included (size, download_url)
            # Optionally, one could add logic here to fetch file content if needed,
            # but that would significantly increase API calls and JSON size.
            pass
        elif item["type"] == "symlink":
            # Handle symbolic links - target might be useful
            node_details["target"] = item.get("target") # The API provides target for symlinks
            print(f"Found symlink: {item['path']} -> {item.get('target')}")
        elif item["type"] == "submodule":
            # Submodules are special directories pointing to other repos
            node_details["submodule_git_url"] = item.get("submodule_git_url")
            print(f"Found submodule: {item['path']} (points to {item.get('submodule_git_url')})")
            # We won't recursively fetch submodule contents here to keep it focused on the main repo.
            # The user can run the script again for the submodule's URL if needed.
        
        tree_nodes.append(node_details)
        
    return tree_nodes

# --- Main Function ---
def github_repo_to_json(repo_url, output_filename="github_repo_structure.json", token=None, max_depth=10):
    """
    Fetches a GitHub repository's structure and saves it as a JSON file.
    """
    try:
        owner, repo_name = parse_github_url(repo_url)
    except ValueError as e:
        print(f"Error: {e}")
        return

    print(f"Processing repository: {owner}/{repo_name}")
    print(f"Max recursion depth set to: {max_depth}")
    if token:
        print("Using GitHub token for authentication.")
    else:
        print("No GitHub token provided. Accessing API anonymously (lower rate limits).")

    # Fetch the main repository information (metadata)
    repo_info_url = f"https://api.github.com/repos/{owner}/{repo_name}"
    print(f"Fetching repository metadata: {repo_info_url}")
    repo_metadata = {}
    try:
        repo_api_data = fetch_github_api(repo_info_url, token)
        if repo_api_data:
            repo_metadata = {
                "id": repo_api_data.get("id"),
                "full_name": repo_api_data.get("full_name"),
                "description": repo_api_data.get("description"),
                "stars": repo_api_data.get("stargazers_count"),
                "forks": repo_api_data.get("forks_count"),
                "language": repo_api_data.get("language"),
                "created_at": repo_api_data.get("created_at"),
                "updated_at": repo_api_data.get("updated_at"),
                "default_branch": repo_api_data.get("default_branch"),
            }
    except requests.exceptions.RequestException as e:
        print(f"Warning: Could not fetch repository metadata: {e}")


    # Fetch the directory structure starting from the root
    # The `fetch_repo_contents_recursive` function will now start by fetching the root contents.
    root_contents = fetch_repo_contents_recursive(owner, repo_name, path="", token=token, max_depth=max_depth)

    # Combine repository metadata with its file/directory structure
    final_repo_structure = {
        "repository_info": {
            "source_url": repo_url,
            "owner": owner,
            "name": repo_name,
            **repo_metadata # Spread the fetched metadata
        },
        "structure_type": "directory_listing",
        "contents": root_contents # This will be the list of files/dirs at the root
    }
    
    if root_contents is None or (isinstance(root_contents, list) and any(item.get("type") == "error" and "Failed to fetch contents" in item.get("message","") for item in root_contents)):
        print("Failed to fetch complete repository structure. The output JSON might be incomplete or represent an error state.")
    
    # Save the JSON output
    try:
        with open(output_filename, "w", encoding="utf-8") as f:
            json.dump(final_repo_structure, f, indent=4, ensure_ascii=False)
        print(f"\nRepository structure successfully saved to {output_filename}")
    except IOError as e:
        print(f"Error writing JSON to file '{output_filename}': {e}")
    except Exception as e:
        print(f"An unexpected error occurred during JSON serialization: {e}")

# --- Command-Line Interface ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert a GitHub repository's file and folder structure to a JSON file.",
        formatter_class=argparse.RawTextHelpFormatter # For better help text formatting
    )
    parser.add_argument(
        "repo_url", 
        help="The URL of the GitHub repository (e.g., https://github.com/owner/repo)."
    )
    parser.add_argument(
        "-o", "--output", 
        default="github_repo_structure.json", 
        help="Output JSON file name (default: github_repo_structure.json)."
    )
    parser.add_argument(
        "-t", "--token", 
        help="GitHub Personal Access Token (PAT) for private repos or to increase API rate limits.\n"
             "You can generate a PAT from your GitHub settings -> Developer settings -> Personal access tokens."
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=10,
        help="Maximum recursion depth for exploring directories (default: 10).\n"
             "Helps prevent issues with extremely deep repositories or circular symlinks."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1, # Default delay of 100ms
        help="Delay in seconds between consecutive API calls (default: 0.1).\n"
             "Increase if you experience rate limiting frequently even with a token."
    )

    args = parser.parse_args()

    # Update global API_CALL_DELAY if specified by user
    API_CALL_DELAY = args.delay
    
    github_repo_to_json(args.repo_url, args.output, args.token, args.max_depth)
