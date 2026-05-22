import zen
import json
import argparse

parser = argparse.ArgumentParser(description="Evaluate a JSON Decision Model.")
parser.add_argument("jdm_filepath", help="Path to the JSON Decision Model (JDM) file.")
parser.add_argument("input_filepath", help="Path to the input data JSON file.")
args = parser.parse_args()

# 1. Load your JSON Decision Model (JDM)
with open(args.jdm_filepath, "r") as f:
    content = f.read()

# 2. Initialize the Engine
engine = zen.ZenEngine()
decision = engine.create_decision(content)

# 3. Evaluate with input data
with open(args.input_filepath, "r") as f:
    input_data = json.load(f)

response = decision.evaluate(input_data)

# 4. Access the Output Node's final result
# The "result" contains exactly what reached the Output Node
result = response["result"]

# 5. Loop through all arrays and find highest priority action
priority_order = ["CP", "IN", "PR", "PU", "TCM", "CR"]
highest_priority_level = -1
highest_priority_items = []

# Find all array fields in the result
for key, value in result.items():
    if isinstance(value, list) and value:  # Non-empty list
        for item in value:
            if isinstance(item, dict) and "action" in item:
                action = item.get("action")
                if action in priority_order:
                    priority_level = priority_order.index(action)
                    if priority_level > highest_priority_level:
                        highest_priority_level = priority_level
                        highest_priority_items = [item]
                    elif priority_level == highest_priority_level:
                        highest_priority_items.append(item)

# Combine results if multiple items at same priority
if highest_priority_items:
    action = highest_priority_items[0]["action"]
    fit_letter = any(item.get("fit_letter", False) for item in highest_priority_items)
    reason = ", and ".join(item.get("reason", "") for item in highest_priority_items)
    
    print(f"Action: {action}")
    print(f"Fit Letter: {fit_letter}")
    print(f"Reason: {reason}")
else:
    print("No actions found")
    
print(f"\nFull result: {result}")
