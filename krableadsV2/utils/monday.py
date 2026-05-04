"""Monday.com GraphQL API integration."""
import requests
import json
from config import Config
from datetime import datetime, timedelta
import pytz
from typing import Optional, Dict, Any


class MondayClient:
    """Monday.com GraphQL API client."""
    
    def __init__(self):
        self.api_url = Config.MONDAY_API_URL
        self.file_api_url = "https://api.monday.com/v2/file"
        self.api_key = Config.MONDAY_API_KEY or ""
        self.board_id = Config.MONDAY_BOARD_ID or ""
        self.headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json"
        }
        self.ny_tz = pytz.timezone("America/New_York")
    
    def is_configured(self) -> bool:
        """Check if Monday.com is properly configured."""
        return bool(self.api_key and self.board_id)
    
    def _get_ny_time(self) -> datetime:
        """Get current time in NY timezone."""
        return datetime.now(self.ny_tz)
    
    def _calculate_expiration(self, issue_date: datetime) -> datetime:
        """Calculate expiration date (issue_date + 30 days)."""
        return issue_date + timedelta(days=30)
    
    def create_item(self, lead_data: Dict[str, Any], telegram_username: str) -> Optional[Dict[str, Any]]:
        """
        Create a new item in Monday.com board.
        
        Fields sent to Monday.com:
        - issue date: auto, NY-Time
        - expiration date: 30 days from issue date
        - Total client paid: price from response
        - picture of our client receipt: null (will be added later)
        - Total on the picture receipt: null
        - Status: "Pending" (changes to "Paid" when receipt picture is filled)
        - name of driver: null (will be added later)
        - full tag info: all of response from user after first bot response (Phase 1)
        - where is the lead from: null
        - whos lead it was: telegram username
        - who dispatched lead: null
        - phone number: plaintext (before encryption)
        - address: extracted from Phase 1 response
        - email: null
        
        Args:
            lead_data: Lead information dictionary (contains phone_number and price)
            telegram_username: Telegram username of the lead creator
            full_tag_info: Full Phase 1 response from user
            
        Returns:
            Dict with item ID and other Monday.com data, or None on error
        """
        # Skip if not configured
        if not self.is_configured():
            return None
        
        issue_date = self._get_ny_time()
        expiration_date = self._calculate_expiration(issue_date)
        
        # Format dates for Monday.com
        # For date columns, Monday expects a JSON object like {"date": "YYYY-MM-DD"}
        issue_date_value = {"date": issue_date.date().isoformat()}
        expiration_date_value = {"date": expiration_date.date().isoformat()}
        
        # Extract core fields from lead_data
        name = lead_data.get('name', '')
        group_message = lead_data.get('group_message', '')
        supervisor_name = lead_data.get('supervisor_name', '')
        phone_number = lead_data.get('phone_number', '')
        price = lead_data.get('price', '').replace('$', '')  # Remove $ sign for Monday.com
        delivery_address = lead_data.get('delivery_address', '')
        delivery_city_state_zip = lead_data.get('delivery_city_state_zip', '')
        full_delivery = ", ".join([part for part in [delivery_address, delivery_city_state_zip] if part]).strip()
        
        # Escape special characters for GraphQL
        def escape_graphql(value: str) -> str:
            return value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
        
        escaped_username = escape_graphql(telegram_username)
        
        # Build complex column values
        phone_value = {"phone": phone_number} if phone_number else None
        # NOTE: Monday.com location columns require a richer structure (lat/lng, etc.).
        # Our current value only has a freeform address, which is causing
        # "invalid value" errors. To keep item creation working, we temporarily
        # disable sending location data until we have the exact structure.
        location_value = None
        
        # Build column values as JSON string (Monday.com expects a JSON string)
        # Map supervisor/group name to DISPATCHER status label (dup__of_lead)
        dispatcher_value = {"label": supervisor_name} if supervisor_name else None

        # Status column (status7) will be left empty on creation; it will be set
        # to "PAID RECEIPT" when a receipt is uploaded.
        status_value = None

        column_values_dict = {
            # Status (status7)
            "status7": status_value,
            # Issue / expiration dates
            "date": issue_date_value,
            "date1": expiration_date_value,
            # Name column: client name from Phase 1
            "name": name,
            # Price column (numeric)
            "numeric_mknfheka": price,
            # Receipt image column (files4) will be updated later when receipt is uploaded
            "files4": None,
            # Driver name column (filled when driver accepts)
            "driver": "",
            # Long text column: same text sent to the group
            "long_text__1": group_message,
            # Supervisor/group info column (DISPATCHER status). We now send a valid label
            # that must already exist on the board, e.g. "Krab group".
            "dup__of_lead": dispatcher_value,
            # Phone number column (phone type)
            "phone": phone_value,
            # Delivery address/location column
            "location__1": location_value,
        }
        column_values_json = json.dumps(column_values_dict)
        escaped_column_values = escape_graphql(column_values_json)
        
        # Build the mutation query (column_values must be a JSON string)
        # NOTE: You'll need to update the column IDs below to match your Monday.com board structure
        # Replace the column IDs (status, date, date1, text, text1, etc.) with your actual Monday.com column IDs
        # 
        # Field mapping:
        # - status: "Pending" (Status column - changes to "Paid" when receipt picture is filled)
        # - date: issue_date (Issue Date column - auto, NY-Time)
        # - date1: expiration_date (Expiration Date column - 30 days from issue date)
        # - text: price (Total client paid column)
        # - text1: "" (picture of our client receipt - null for now, will be added later)
        # - text2: "" (Total on the picture receipt - null)
        # - text3: telegram_username (whos lead it was)
        # - text4: phone_number (phone number - plaintext before encryption)
        # - text5: full_tag_info (full tag info - all of Phase 1 response)
        # - text6: "" (name of driver - null for now, will be added later)
        # - text7: "" (where is the lead from - null)
        # - text8: "" (who dispatched lead - null)
        # - text9: "" (address - extracted from Phase 1, if needed)
        # - text10: "" (email - null)
        
        mutation = f"""
        mutation {{
            create_item(
                board_id: {self.board_id},
                item_name: "Lead from @{escaped_username}",
                column_values: "{escaped_column_values}"
            ) {{
                id
                name
            }}
        }}
        """
        
        try:
            response = requests.post(
                self.api_url,
                headers=self.headers,
                json={"query": mutation},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                if "errors" in data:
                    print(f"Monday.com GraphQL errors: {data['errors']}")
                    return None
                
                item_data = data.get("data", {}).get("create_item", {})
                return {
                    "item_id": item_data.get("id"),
                    "name": item_data.get("name"),
                    "issue_date": issue_date,
                    "expiration_date": expiration_date
                }
            else:
                print(f"Monday.com API error: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            print(f"Error creating Monday.com item: {e}")
            return None
    
    def update_item_status(self, item_id: int, status: str) -> bool:
        """
        Update item status in Monday.com.
        
        Args:
            item_id: Monday.com item ID
            status: New status (e.g., "Paid")
            
        Returns:
            True if successful, False otherwise
        """
        # Skip if not configured
        if not self.is_configured():
            return False
        
        # Status column expects a JSON string value, e.g. "{\"label\":\"Paid\"}"
        status_dict = {"label": status}
        status_json = json.dumps(status_dict)
        safe_status_json = status_json.replace('\\', '\\\\').replace('"', '\\"')
        
        mutation = f"""
        mutation {{
            change_column_value(
                board_id: {self.board_id},
                item_id: {item_id},
                column_id: "status7",
                value: "{safe_status_json}"
            ) {{
                id
            }}
        }}
        """
        
        try:
            response = requests.post(
                self.api_url,
                headers=self.headers,
                json={"query": mutation},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                if "errors" in data:
                    print(f"Monday.com GraphQL errors: {data['errors']}")
                    return False
                return True
            else:
                print(f"Monday.com API error: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            print(f"Error updating Monday.com item status: {e}")
            return False
    
    def update_item_driver(self, item_id: int, driver_name: str) -> bool:
        """
        Update the driver name column in Monday.com when a driver accepts a lead.
        """
        # Skip if not configured
        if not self.is_configured():
            return False
        
        # Escape driver name for GraphQL
        safe_name = driver_name.replace('\\', '\\\\').replace('"', '\\"')
        
        mutation = f"""
        mutation {{
            change_column_value(
                board_id: {self.board_id},
                item_id: {item_id},
                column_id: "driver",
                value: "{{\\"text\\": \\"{safe_name}\\"}}"
            ) {{
                id
            }}
        }}
        """
        
        try:
            response = requests.post(
                self.api_url,
                headers=self.headers,
                json={"query": mutation},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                if "errors" in data:
                    print(f"Monday.com GraphQL errors: {data['errors']}")
                    return False
                return True
            else:
                print(f"Monday.com API error: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            print(f"Error updating Monday.com driver column: {e}")
            return False

    def update_item_contact_source(self, item_id: int, contact_source_label: str) -> bool:
        """Update the contact info source column (text_mm0ske02) on the Monday board.
        Text columns require change_simple_column_value with a plain string value, not JSON."""
        if not self.is_configured():
            return False
        # Escape for GraphQL string: backslash and double-quote
        safe = (contact_source_label or "").replace('\\', '\\\\').replace('"', '\\"')
        mutation = f"""
        mutation {{
            change_simple_column_value(
                board_id: {self.board_id},
                item_id: {item_id},
                column_id: "text_mm0ske02",
                value: "{safe}"
            ) {{
                id
            }}
        }}
        """
        try:
            response = requests.post(
                self.api_url,
                headers=self.headers,
                json={"query": mutation},
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                if "errors" in data:
                    print(f"Monday.com GraphQL errors (contact source): {data['errors']}")
                    return False
                return True
            return False
        except Exception as e:
            print(f"Error updating Monday.com contact source column: {e}")
            return False

    def update_item_receipt(self, item_id: int, file_name: str, file_content: bytes) -> bool:
        """
        Upload a receipt image file to the Monday.com files4 column for the item.
        
        Args:
            item_id: Monday.com item ID
            file_name: Name of the receipt file (e.g. "receipt.jpg")
            file_content: Raw bytes of the receipt image
            
        Returns:
            True if successful, False otherwise
        """
        # Skip if not configured
        if not self.is_configured():
            return False
        
        # Monday.com requires a separate file upload endpoint for file columns.
        # We use the add_file_to_column mutation via the /v2/file endpoint.
        #
        # IMPORTANT: The file upload MUST follow the GraphQL multipart request
        # spec, which Monday.com implements. That means:
        #   - The raw file goes in a separate part (e.g. field name "image")
        #   - The "map" field maps that part to "variables.file"
        #   - The "variables" field includes a "file" variable placeholder
        #
        # The previous implementation was sending the file as
        # "variables[file]" without a "map" field, which causes Monday.com
        # to ignore the file and fail validation.
        query = """
        mutation ($file: File!, $item_id: ID!, $column_id: String!) {
          add_file_to_column (file: $file, item_id: $item_id, column_id: $column_id) {
            id
          }
        }
        """
        # "file" is a placeholder; the actual bytes are provided via multipart.
        # NOTE: Monday's schema expects item_id as ID!, so we pass it as a string.
        variables = {
            "file": None,
            "item_id": str(item_id),
            "column_id": "files4",
        }
        
        # Multipart form structure:
        # - "query": GraphQL mutation
        # - "variables": JSON with item_id, column_id, file: null
        # - "map": links file part "image" to "variables.file"
        # - "image": the actual file content
        data = {
            "query": query,
            "variables": json.dumps(variables),
            "map": json.dumps({"image": "variables.file"}),
        }
        files = {
            "image": (file_name or "receipt.jpg", file_content),
        }
        
        try:
            response = requests.post(
                self.file_api_url,
                headers={
                    "Authorization": self.api_key,
                },
                data=data,
                files=files,
                timeout=20,
            )
            
            if response.status_code == 200:
                data = response.json()
                if "errors" in data:
                    print(f"Monday.com GraphQL errors: {data['errors']}")
                    return False
                return True
            else:
                print(f"Monday.com file API error: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            print(f"Error uploading Monday.com receipt file: {e}")
            return False

    def update_item_receipt_link(self, item_id: int, receipt_url: str) -> bool:
        """
        Fallback: store a public receipt URL in a text/long-text column
        when direct file upload fails.

        This writes the URL into the long_text__1 column so the team still
        has a clickable link to the receipt image (e.g. Supabase public URL).
        """
        if not self.is_configured():
            return False
        if not receipt_url:
            return False

        # Escape for GraphQL JSON string
        safe_text = receipt_url.replace('\\', '\\\\').replace('"', '\\"')

        mutation = f"""
        mutation {{
            change_column_value(
                board_id: {self.board_id},
                item_id: {int(item_id)},
                column_id: "long_text__1",
                value: "{{\\"text\\": \\"{safe_text}\\"}}"
            ) {{
                id
            }}
        }}
        """

        try:
            response = requests.post(
                self.api_url,
                headers=self.headers,
                json={"query": mutation},
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                if "errors" in data:
                    print(f"Monday.com GraphQL errors (receipt link): {data['errors']}")
                    return False
                return True
            else:
                print(f"Monday.com API error (receipt link): {response.status_code} - {response.text}")
                return False
        except Exception as e:
            print(f"Error updating Monday.com receipt link: {e}")
            return False

