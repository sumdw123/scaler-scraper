# Apache Jira LLM Corpus Scraper

This project contains a Python-based data pipeline to scrape public issue data from Apache's Jira instance (`https://issues.apache.org/jira`). The system is designed to be fault-tolerant, resumable, and efficient. It extracts issue metadata, descriptions, and comments, then transforms this data into a structured JSONL format suitable for training Large Language Models (LLMs).

## 🏛️ Architecture Overview and Design Reasoning

The core design decision was to **use the official Jira REST API instead of HTML scraping**.

This approach was chosen for several key reasons:
1.  **Reliability:** The API provides clean, structured JSON data. This completely avoids the fragile process of parsing HTML, which breaks every time a website's layout changes.
2.  **Efficiency:** We can request only the specific fields we need (`summary`, `description`, `comment`, etc.), making our network requests small and fast.
3.  **Politeness:** Using the API is the officially sanctioned way to interact with the data and is less likely to be blocked than an aggressive HTML scraper.
4.  **Data Richness:** The API provides rich metadata (timestamps, reporters, status history) that is difficult or impossible to get from HTML.

### Pipeline Flow

1.  **Initialize:** The script loads a `scraper_state.json` file to determine which project and which page (index) to start from. This makes it **resumable**.
2.  **Create Session:** A `requests.Session` is created with an `HTTPAdapter` that has a built-in `Retry` strategy. This session will automatically handle and retry on `429 (Too Many Requests)` and `5xx (Server Error)` responses, ensuring **fault tolerance**.
3.  **Paginate and Fetch:** For each project, the script makes `GET` requests to the `/rest/api/latest/search` endpoint.
    * It uses **JQL** (Jira Query Language) to filter by project (e.g., `project = SPARK`).
    * It handles pagination using the `startAt` and `maxResults` parameters.
4.  **Transform:** For each issue received, the JSON is passed to a `transform_issue_for_llm` function. This function:
    * Flattens the nested JSON into a clean, top-level structure.
    * Concatenates all issue comments into a single text field.
    * Generates "derived tasks" (Summarization, Classification, Q&A) formatted with `instruction`, `input`, and `output` keys, making the data immediately usable for LLM fine-tuning.
5.  **Write and Save State:**
    * The transformed JSON object is appended as a new line to `output.jsonl` (the JSON Lines format).
    * After each *batch* of issues is successfully written to disk, the `scraper_state.json` file is updated with the new `startAt` index. If the script is interrupted, it can restart from this exact spot.

---

## ⚙️ Setup and Installation

### Requirements
* Python 3.8+
* `requests`
* `tqdm`

### Instructions

1.  Clone the repository:
    ```bash
    git clone https://github.com/sumdw123/scaler-scraper.git
    cd scaler-scraper
    ```

2.  Create and activate a Python virtual environment:
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Unix/macOS
    .\venv\Scripts\activate   # On Windows
    ```

3.  Install the required packages:
    ```bash
    pip install -r requirements.txt
    ```

4.  Run the scraper:
    ```bash
    python scrape_jira.py
    ```
    The script will print its progress to the console and create/append to `output.jsonl`. You can stop the script (Ctrl+C) and restart it, and it will pick up where it left off.

---

## 🚨 Edge Cases and Reliability Handled

The system is designed to be robust against real-world data and network issues.

* **Request Failures & Timeouts:** Handled by the `requests.Session` and `Retry` strategy. The session will automatically retry failed requests with an exponential backoff.
* **HTTP 429 & 5xx Responses:** Explicitly included in the `Retry` strategy's `status_forcelist`. The scraper will automatically back off and retry when it hits a rate limit or a server error.
* **Empty or Malformed Data:** The `transform_issue_for_llm` function is wrapped in a `try...except` block. If an issue is missing a key field (e.g., `fields` is `null` or `summary` is missing), the script will log the error and skip that single issue rather than crashing the entire pipeline. It also handles `None` or empty `description` fields gracefully.
* **Interruption & Resumability:** The scraper state (`current_project_index`, `current_startAt`) is saved to `scraper_state.json` after *every* successful batch. If the script is stopped for any reason (network loss, user interruption, server crash), it can be restarted and will resume from the exact last successful batch, preventing duplicate work and data loss.
* **Pagination:** Handled by looping and incrementing the `startAt` parameter until the number of fetched issues is less than `maxResults` or the `issues` array is empty, or `startAt` exceeds the `total` issues reported by the API.

---

## 🚀 Optimization and Future Improvements

### Optimizations Implemented

1.  **Connection Pooling:** By using `requests.Session`, we reuse the underlying TCP connection for multiple requests to the same host, which significantly reduces latency.
2.  **Targeted Field Selection:** We use the `fields` parameter in the API call to request *only* the data we need. This dramatically reduces payload size and improves API response time compared to fetching the full issue object.
3.  **Append-Only Writing:** The output file `output.jsonl` is opened in append (`'a'`) mode. This is highly efficient as we don't need to load the entire dataset into memory to add new entries.

### Potential Future Improvements

1.  **Concurrent Scraping:** The current script scrapes projects sequentially. A future version could use `asyncio` with `aiohttp` or Python's `multiprocessing` to scrape all 3 projects in parallel, drastically cutting down the total time.
2.  **Better Text Cleaning:** The current script saves the raw Jira markup (e.g., `*bold*`, `{code}...{code}`). A future improvement would be to use a simple regex or a dedicated library to convert this markup to clean Markdown or plain text, which might be better for some LLM training tasks.
3.  **Dynamic Rate Limiting:** Instead of a fixed backoff, we could read the `Retry-After` header on a `429` response and sleep for the *exact* time specified by the server.

4.  **Database Integration:** For a very large-scale system, writing directly to a database (like PostgreSQL or MongoDB) instead of a JSONL file would be more robust and allow for easier querying of the results.
