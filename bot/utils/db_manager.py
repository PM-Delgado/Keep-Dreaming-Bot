import json
import random
import os
from datetime import datetime, timedelta, timezone

class DBManager:
    @staticmethod
    def get_current_name():
        """Get the current name of the day if it's still valid"""
        try:
            with open('bot/data/name_of_the_day.json', 'r') as f:
                data = json.load(f)
                
            if data['name'] and data['last_updated']:
                last_updated = datetime.fromisoformat(data['last_updated']).astimezone()
                now = datetime.now(timezone.utc).astimezone()
                # Reset both times to midnight for accurate daily comparison
                if last_updated.replace(hour=0, minute=0, second=0, microsecond=0) == now.replace(hour=0, minute=0, second=0, microsecond=0):
                    return data['name']
                    
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
        return None
    
    @staticmethod
    def get_current_phrase():
        """Get the current name of the day if it's still valid"""
        try:
            with open('bot/data/name_of_the_day.json', 'r') as f:
                data = json.load(f)
                
            if data['name'] and data['last_updated']:
                last_updated = datetime.fromisoformat(data['last_updated']).astimezone()
                now = datetime.now(timezone.utc).astimezone()
                # Reset both times to midnight for accurate daily comparison
                if last_updated.replace(hour=0, minute=0, second=0, microsecond=0) == now.replace(hour=0, minute=0, second=0, microsecond=0):
                    return data['phrase']
                    
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
        return None

    @staticmethod
    def update_name_of_the_day():
        """Select a new random name and update statistics"""
        try:
            # Get all names
            with open('bot/data/names.json', 'r', encoding='utf-8') as f:
                names = json.load(f)

            # Get all phrases
            with open('bot/data/phrases.json', 'r', encoding='utf-8') as f:
                phrases = json.load(f)

            if not names or not phrases:
                return None
                
            # Select random name
            selected_name = random.choice(names)
            selected_phrase = random.choice(phrases).replace("{name}", selected_name)
            
            # Update stats
            with open('bot/data/name_stats.json', 'r+', encoding='utf-8') as f:
                stats = json.load(f)
                stats[selected_name] = stats.get(selected_name, 0) + 1
                f.seek(0)
                json.dump(stats, f)
                f.truncate()
                
            # Update current name
            current_data = {
                'name': selected_name,
                'last_updated': datetime.now(timezone.utc).astimezone().isoformat(),
                'phrase': selected_phrase
            }
            
            with open('bot/data/name_of_the_day.json', 'w', encoding='utf-8') as f:
                json.dump(current_data, f)
                
            return selected_name
            
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Error updating name: {e}")
            return None

    @staticmethod
    def get_leaderboard():
        """Get top 3 names by selection count"""
        try:
            with open('bot/data/name_stats.json', 'r') as f:
                stats = json.load(f)
                
            sorted_names = sorted(stats.items(), key=lambda x: x[1], reverse=True)
            return sorted_names[:3]
            
        except (FileNotFoundError, json.JSONDecodeError):
            return []
