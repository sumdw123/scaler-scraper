import requests
import json
import os
import time
import logging
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from tqdm import tqdm

# --- Configuration ---

# The base URL for Apache's public Jira API
BASE_URL = "https://issues.apache.org/jira/rest/api/latest/search"

# Choose any 3 projects. I've picked Spark, Kafka, and Beam.
# You can find other keys like 'HADOOP', 'FLINK', 'ARROW' etc.
PROJECTS_TO_SCRAPE = ["SPARK", "KAFKA", "BEAM"]

# Fields we want to extract. Specifying fields makes the API call much faster.
# 'comment' is included to get all comments.
FIELDS_TO_EXTRACT = "summary,description,status,priority,reporter,assignee,created,updated,labels,issuetype,comment"

# Issues to fetch per API call (Jira's max is typically 100)
MAX_RESULTS_PER_PAGE = 100

# File to store our progress
STATE_FILE = "scraper_state.json"

# Final output file
OUTPUT_FILE = "output.jsonl"

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- End Configuration ---


def create_fault_tolerant_session():
    """
    Creates a requests.Session with automatic retries for rate limits and server errors.
    This directly handles "HTTP 429 and 5xx responses" and "Request failures".
    """
    session = requests.Session()
    
    # Define the retry strategy
    retry_strategy = Retry(
        total=5,  # Total number of retries
        status_forcelist=[429, 500, 502, 503, 504],  # HTTP codes to retry on
        backoff_factor=1,  # Will sleep for {backoff_factor} * (2 ** ({number_of_retries} - 1))
        allowed_methods=["GET"]
    )
    
    # Mount the strategy to the session
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    return session

def load_state():
    """
    Loads the last saved state from STATE_FILE.
    This enables the scraper to "Resume from the last successful state".
    """
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                logging.warning("State file is corrupt. Starting from scratch.")
                return {"current_project_index": 0, "current_startAt": 0}
    return {"current_project_index": 0, "current_startAt": 0}

def save_state(project_index, start_at):
    """Saves the current progress to the STATE_FILE."""
    state = {
        "current_project_index": project_index,
        "current_startAt": start_at
    }
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def transform_issue_for_llm(issue, project_key):
    """
    Transforms the raw Jira JSON issue into a structured JSONL format
    and creates derived tasks for LLM training.
    """
    try:
        fields = issue.get('fields', {})
        # Handle if the entire 'fields' object is missing or null
        if not fields:
            logging.warning(f"Issue {issue.get('key')} has no 'fields'. Skipping.")
            return None

        # 1. Clean and extract metadata
        issue_key = issue.get('key')
        title = fields.get('summary', 'No Title')
        
        # Handle potentially 'None' or empty descriptions
        description = fields.get('description') if fields.get('description') else "No Description Provided"

        # --- ROBUST FIX for 'NoneType' error ---
        # Handle cases where the field exists but is 'null' (None in Python)
        
        status_obj = fields.get('status')
        status = status_obj.get('name', 'Unknown') if status_obj else 'Unknown'
        
        priority_obj = fields.get('priority')
        priority = priority_obj.get('name', 'Unknown') if priority_obj else 'Unknown'
        
        reporter_obj = fields.get('reporter')
        reporter = reporter_obj.get('displayName', 'Unknown') if reporter_obj else 'Unknown'
        
        assignee_obj = fields.get('assignee')
        assignee = assignee_obj.get('displayName', 'Not Assigned') if assignee_obj else 'Not Assigned'
        
        # --- END FIX ---

        project = project_key
        created = fields.get('created', '')
        labels = fields.get('labels', [])

        # 2. Consolidate all comments into a single text block
        all_comments = []
        # Check if 'comment' field itself exists and is not null
        comment_data = fields.get('comment')
        if comment_data and 'comments' in comment_data:
            for comment in comment_data['comments']:
                # Adding author to provide context, like in a real forum
                author_obj = comment.get('author')
                author = author_obj.get('displayName', 'Unknown') if author_obj else 'Unknown'
                body = comment.get('body', '').strip()
                if body:
                    all_comments.append(f"Comment by {author}:\n{body}\n---")
        
        comments_text = "\n".join(all_comments) if all_comments else "No Comments"

        # 3. Create the structured JSON object
        structured_data = {
            "id": issue_key,
            "project": project,
            "title": title,
            "description": description,
            "comments_text": comments_text,
            "metadata": {
                "status": status,
                "priority": priority,
                "reporter": reporter,
                "assignee": assignee,
                "created_at": created,
                "labels": labels,
                "issue_url": f"https://issues.apache.org/jira/browse/{issue_key}"
            },
            
            # 4. Create "Derived tasks" for LLM training
            "derived_tasks": [
                {
                    "task_type": "summarization",
                    "instruction": "Summarize the following software issue, including its description and all comments, into a concise one-sentence title.",
                    "input": f"Description:\n{description}\n\nComments:\n{comments_text}",
                    "output": title
                },
                {
                    "task_type": "classification",
                    "instruction": "Based on the issue title and description, classify its priority. Valid options are: Blocker, Critical, Major, Minor, Trivial, Unknown.",
                    "input": f"Title: {title}\nDescription: {description}",
                    "output": priority
                },
                {
                    "task_type": "question_answering",
                    "instruction": "What is the status of this issue?",
                    "input": f"Title: {title}\nDescription: {description}\nComments:\n{comments_text}",
                    "output": status
                }
            ]
        }
        return structured_data

    except Exception as e:
        # Log the specific issue key if possible
        logging.error(f"Failed to transform issue {issue.get('key')}: {e}")
        return None # Handle malformed data by skipping it

def fetch_issues():
    """Main function to fetch issues, handle pagination, and save data."""
    
    session = create_fault_tolerant_session()
    state = load_state()
    
    start_project_index = state['current_project_index']
    
    for i in range(start_project_index, len(PROJECTS_TO_SCRAPE)):
        project_key = PROJECTS_TO_SCRAPE[i]
        
        # If we're starting this project, begin at 0.
        # If we're resuming, start at the saved 'current_startAt'.
        start_at = state['current_startAt'] if i == start_project_index else 0
        
        logging.info(f"--- Starting project: {project_key} (Resuming from index {start_at}) ---")
        
        # We use a 'with' block to ensure the output file is closed
        # properly if the script is interrupted. 'a' means append.
        with open(OUTPUT_FILE, 'a', encoding='utf-8') as f:
            
            # We need to get the total number of issues first to set up our progress bar
            try:
                total_issues = get_total_issues(session, project_key)
                if total_issues == 0:
                    logging.warning(f"Project {project_key} has no issues or is inaccessible. Skipping.")
                    continue
            except Exception as e:
                logging.error(f"Could not get total for project {project_key}: {e}. Skipping.")
                continue

            # Initialize progress bar
            pbar = tqdm(total=total_issues, desc=f"Scraping {project_key}", initial=start_at)
            
            while True:
                # JQL (Jira Query Language) to search for all issues in the project
                jql = f"project = {project_key} ORDER BY created ASC"
                
                params = {
                    'jql': jql,
                    'startAt': start_at,
                    'maxResults': MAX_RESULTS_PER_PAGE,
                    'fields': FIELDS_TO_EXTRACT,
                    'expand': 'comment' # We need to expand to get comments
                }
                
                try:
                    response = session.get(BASE_URL, params=params)
                    
                    # Handle non-200 responses that weren't retried
                    if response.status_code != 200:
                        logging.error(f"Failed to fetch data: {response.status_code} - {response.text}")
                        # If it's a 429, we might have exhausted retries. Sleep and try this batch again.
                        if response.status_code == 429:
                            logging.warning("Rate limit hit. Sleeping for 60 seconds...")
                            time.sleep(60)
                            continue # Retry this same batch
                        else:
                            break # Break for other critical errors

                    data = response.json()
                    issues = data.get('issues', [])
                    
                    if not issues:
                        # No more issues to fetch
                        logging.info(f"No more issues found for {project_key} at index {start_at}.")
                        break
                    
                    # Process and write each issue to the JSONL file
                    for issue in issues:
                        transformed_data = transform_issue_for_llm(issue, project_key)
                        if transformed_data: # Skip malformed data
                            f.write(json.dumps(transformed_data) + '\n')
                    
                    # Update our position
                    start_at += len(issues)
                    pbar.update(len(issues))
                    
                    # Save our state *after* successfully processing and writing the batch
                    save_state(i, start_at)

                    # Check if we've fetched all issues
                    total_from_response = data.get('total', 0)
                    if start_at >= total_from_response:
                        logging.info(f"Successfully fetched all {total_from_response} issues for {project_key}.")
                        break
                
                except requests.RequestException as e:
                    logging.error(f"A network error occurred: {e}. Retrying after 30s...")
                    time.sleep(30)
                except Exception as e:
                    logging.error(f"An unexpected error occurred: {e}. Skipping batch.")
                    # Move to the next batch to avoid getting stuck
                    start_at += MAX_RESULTS_PER_PAGE
                    pbar.update(MAX_RESULTS_PER_PAGE)


            pbar.close()
            
        # Reset 'startAt' for the next project
        save_state(i + 1, 0)

    logging.info("--- All projects scraped successfully! ---")

def get_total_issues(session, project_key):
    """Helper function to get the total issue count for a project."""
    jql = f"project = {project_key}"
    params = {'jql': jql, 'maxResults': 0} # We only want the total count
    response = session.get(BASE_URL, params=params)
    response.raise_for_status() # Will raise an error for bad responses
    return response.json().get('total', 0)


if __name__ == "__main__":
    fetch_issues()