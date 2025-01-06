<!-- MACOS -

python3 -m venv venv

source venv/bin/activate

python main.py dev -->


# Project Setup and Execution Guide

This guide will walk you through setting up the environment and executing the necessary Python scripts for this project.

## Prerequisites

- Python 3.7+ must be installed on your machine.
- You should have **pip** (Python package installer) available.
- Visual Studio Code (VS Code) or any other code editor of your choice.
- Command-line access via **PowerShell** or **Command Prompt** (Windows) or **Terminal** (macOS/Linux).

## Steps to Set Up and Execute the Python Files

### 1. **Clone the Repository (if applicable)**

If you haven't already cloned the repository, start by cloning it from the source repository:

```bash
git clone <repository-url>
cd <repository-folder>
```

### 2. **Create a Virtual Environment**

To ensure you are using an isolated environment for dependencies, create a virtual environment. In your project folder, run:

```bash
python -m venv myenv
```

This command will create a new folder named `myenv` which will hold the Python virtual environment.

### 3. **Activate the Virtual Environment**

Once the virtual environment is created, you need to activate it. The activation process differs depending on the terminal you are using:

- **For PowerShell (default in VS Code on Windows):**
  ```bash
  .\myenv\Scripts\Activate.ps1
  ```
- **For Command Prompt (if you're using Command Prompt on Windows):**
  ```bash
  .\myenv\Scripts\activate.bat
  ```

After activation, you should see the environment name (`myenv`) in your terminal prompt, indicating that the virtual environment is active.

### 4. **Install the Required Dependencies**

Next, install the necessary dependencies by running the following command:

```bash
pip install -r requirements.txt
```

This will install all the Python packages listed in the `requirements.txt` file for your project.

### 5. **Run the Python Scripts**

After setting up the environment and installing the required packages, you can run the Python scripts. Use the following commands:

- **To download the required files**:

  ```bash
  python main.py download-files
  ```

- **To run the development script**:
  ```bash
  python main.py dev
  ```

Note: If you're using a Unix-based system like Linux or macOS, replace `python` with `python3` to run the commands, e.g.:

```bash
python3 main.py download-files
python3 main.py dev
```

### 6. **Deactivate the Virtual Environment**

Once you're done working with the virtual environment, you can deactivate it by running:

```bash
deactivate
```

---

### Troubleshooting

- **Issue: "python3: command not found"**
  - On Windows, you should use `python` instead of `python3` to execute Python scripts.
- **Issue: "pip: command not found"**
  - Make sure that Python is correctly installed and that `pip` is added to your PATH. If not, reinstall Python and ensure to check the "Add Python to PATH" box during installation.

---

This guide should help you get started with setting up and running the Python scripts. If you encounter any issues, feel free to check the project's documentation or contact the project maintainers.
