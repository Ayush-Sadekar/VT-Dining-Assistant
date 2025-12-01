import modal
import subprocess
import time
from modal import build, enter, method
import os

MODEL = os.environ.get("MODEL", "llama3.2")

def pull(model: str = MODEL):
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "ollama"], check=True)
    subprocess.run(["systemctl", "start", "ollama"], check=True)
    time.sleep(2)  # Wait for the service to start
    subprocess.run(["ollama", "pull", model], stdout=subprocess.PIPE, check=True)


image = modal.Image.debian_slim(python_version="3.12").apt_install(
    "curl",    
    "systemctl"
).run_commands(
    "curl -L https://github.com/ollama/ollama/releases/download/v0.6.2/ollama-linux-amd64.tgz -o ollama-linux-amd64.tgz",
    "tar -C /usr -xzf ollama-linux-amd64.tgz",
    "useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama",
    "usermod -a -G ollama $(whoami)",
).add_local_file(
    "ollama.service", "/etc/systemd/system/ollama.service", copy=True
).pip_install(
    "flask",
    "chromadb",
    "datetime",
    "beautifulsoup4",
    "ollama",
    "requests"
).add_local_python_source(
    "scraper",
    "LLM_stuff",
    copy=True
).add_local_dir(
    "/Users/ayush/Desktop/BeautSoupCrawlerDining/scripts", remote_path="/root", copy=True
).add_local_dir(
    "/Users/ayush/Desktop/BeautSoupCrawlerDining/scripts/DiningHalls", remote_path="/root/DiningHalls", copy=True
).run_function(
    pull, force_build=True
)

app = modal.App("VT-Dining-Assistant", image=image)

with image.imports():
    import ollama

MINUTES = 60

# @app.cls(
#     gpu="A10",
#     scaledown_window=5 * MINUTES,
#     timeout=60 * MINUTES,
#     volumes={
#         "/cache": modal.Volume.from_name("hf-hub-cache", create_if_missing=True),
#     },
# )

@app.cls()
class Ollama:
    @modal.enter()
    def enter(self):
        print("Starting the ollama service")
        subprocess.run(["systemctl", "start", "ollama"], check=True)

    @modal.method()
    def infer(self, fields:list) -> str:
        import ollama

        response = ollama.generate(model=MODEL,
                                   prompt=(
                                       f"Context:\n{fields[1]}\n\n"
                                        f"Query: {fields[0]}\n\n"
                                        "Instructions:\n"
                                        "1. Answer using only the provided context\n"
                                        "2. Be specific about hall names and ingredients\n"
                                        "3. If unsure, request clarification\n"
                                        "4. Mention calorie counts and protein values when relevant\n"
                                        "5. If any float values have more than 2 decimal points, round it back down to 2 decimal points.\n"
                                        "Answer:"
                                   ),stream=False)
        return response["response"]
    @modal.method()
    def simple_generate(self, food_item:str) -> str:
        import ollama

        response = ollama.generate(
            model=MODEL,
            prompt=(
                f"Food Item: {food_item}\n"
                "Instructions:\n"
                "For the food item above, determine ingredients that could be used to make it.\n"
                "Be specific about the names of the ingredients\n"
                "List the ingredients separated by commas\n"
            ),
            stream=False
        )
        return response["response"]
    

@app.function(image=image)
@modal.concurrent(max_inputs=100)
@modal.wsgi_app()
def flask_app():
    import chromadb
    from datetime import date
    import os
    from scraper import scrape_vt_dining_locations, get_item_and_metadata
    from LLM_stuff import query_func, process_data, query_func_messages
    from flask import Flask, request, jsonify, render_template
    import ollama
    from bs4 import BeautifulSoup
    import requests
    import random

    print(f"The current directory is {os.getcwd()}")
    print(f"{os.listdir()}")

    dir_path = "/Users/ayush/Desktop/BeautSoupCrawlerDining/scripts"
    date_path = os.path.join(dir_path, "date.txt")
    chroma_path = os.path.join(dir_path, "ChromaClient")
    my_ollama = Ollama()

    chroma_client = chromadb.PersistentClient(path=chroma_path)

    dateText = ""

    try:
        with open(date_path, 'r', encoding='utf-8') as file:
            dateText = file.read()
    except FileNotFoundError:
        dateText = ""

    today = date.today()
    date_string = today.strftime("%Y-%m-%d")

    collection = None

    if (date_string != dateText):

        with open(date_path, 'w') as date_file:
            date_file.write(date_string)
        
        try:

            dining_halls = scrape_vt_dining_locations("https://foodpro.students.vt.edu/menus/")

            chroma_client = chromadb.PersistentClient(path=chroma_path)
            collection = chroma_client.get_or_create_collection("Dining_Collection")

            current_id = 0

            for hall in dining_halls:
                hall_dict = get_item_and_metadata(hall)
                current_id = process_data(collection, hall_dict, current_id)
            
            #query = input("What are your nutrition goals for today?\n>>>")
            #query_func(query, collection)

        except Exception as e:
            print(f"Error updating dining information: {e}")
    
    
    chroma_client = chromadb.PersistentClient(path=chroma_path)
    collection = chroma_client.get_collection("Dining_Collection")

    url = "https://foodpro.students.vt.edu/menus/"

    locations = scrape_vt_dining_locations(url)
    hall_names = []

    for loc in locations: 
        response = requests.get(loc)
        soup = BeautifulSoup(response.text, 'html.parser')

        hall_name = soup.find(id="dining_center_name_container").text.strip()
        if "/" in hall_name:
            hall_name = hall_name.replace("/", "or")
        hall_names.append(hall_name)

    item_dict = {}

    for hall in hall_names:

        prim_path = os.getcwd()
        end_path = "DiningHalls"
        path = os.path.join(prim_path, end_path)
        path = os.path.join(path, hall)
        path += ".txt"

        try:
            with open(path, "r") as file:
                lines = file.readlines()
        except:
            continue
        
        name = hall
        
        for i in range(1, len(lines), 2): 
            
            mtadata = {}

            name_line = lines[i]
            calories = lines[i+1]

            name_line = name_line.replace("(", "")
            name_line = name_line.replace(": Calories", "")
            name_line = name_line.replace("\n", "")

            calories = calories.replace("                                      ", "")
            calories = calories.replace(" protein unavailable)", "")
            calories = calories.replace("\n", "")

            protein = random.uniform(15.0, 45.0)
            protein = str(protein)


            day = date.today().strftime("%Y-%m-%d")

            sample_ingredients = [
                # Vegetarian options
                "spinach, feta, tomatoes, olives",
                "mushrooms, garlic, basil, olive oil",
                "tofu, bell peppers, soy sauce, ginger",
                
                # Non-vegetarian options
                "chicken, onions, carrots, thyme",
                "beef, potatoes, rosemary, butter",
                "salmon, lemon, dill, capers"
            ]

            mtadata["Calories"] = calories
            mtadata["Protein"] = f"{protein}g"
            mtadata["Date"] = day
            mtadata["Location"] = name
            mtadata["Dish"] = name_line
            mtadata["Ingredients"] = sample_ingredients[random.randint(0,5)]

            item_dict[name_line] = mtadata

    chroma_path = os.path.join("/Users/ayush/Desktop/BeautSoupCrawlerDining/scripts", "ChromaClient")
    chroma_client = chromadb.PersistentClient(path=chroma_path)

    collection = chroma_client.get_or_create_collection("Dining_Collection")

    process_data(collection=collection, ticker="newlyenriched", item_dict=item_dict, current_id=0)
    
    web_app = Flask(__name__)

    @web_app.route('/')
    def home():
        print(f"The current directory is {os.getcwd()}")
        print(f"{os.listdir()}")        
        return render_template('index.html')

    @web_app.route('/api/query', methods = ['POST'])
    def query():
        data = request.json
        query_text = data.get('query', '')

        if not query_text:
            return jsonify({'response':'Please enter a query'}), 400
        else:
            return jsonify({'response': 
                            my_ollama.infer.remote(
                                query_func_messages(query_text, collection)
                                )
                            })
    
    
    return web_app
    #if __name__ == '__main__':

        #port = int(os.environ.get('PORT', 5050))

        #app.run(host='0.0.0.0', port=port, debug=False)

        
