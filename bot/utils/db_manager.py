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
                
            if data['current_name'] and data['last_updated']:
                last_updated = datetime.fromisoformat(data['last_updated']).astimezone()
                now = datetime.now(timezone.utc).astimezone()
                # Reset both times to midnight for accurate daily comparison
                if last_updated.replace(hour=0, minute=0, second=0, microsecond=0) == now.replace(hour=0, minute=0, second=0, microsecond=0):
                    return data['current_name']
                    
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
        return None

    @staticmethod
    def update_name_of_the_day():
        """Select a new random name and update statistics"""
        try:
            # Get all names
            with open('bot/data/names.json', 'r') as f:
                names = json.load(f)
                
            if not names:
                return None
                
            # Select random name
            selected = random.choice(names)
            
            # Update stats
            with open('bot/data/name_stats.json', 'r+') as f:
                stats = json.load(f)
                stats[selected] = stats.get(selected, 0) + 1
                f.seek(0)
                json.dump(stats, f)
                f.truncate()
                
            # Update current name
            current_data = {
                'current_name': selected,
                'last_updated': datetime.now(timezone.utc).astimezone().isoformat()
            }
            
            with open('bot/data/name_of_the_day.json', 'w') as f:
                json.dump(current_data, f)
                
            return selected
            
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

    @staticmethod
    def add_name(name: str):
        """Add a new name to the pool"""
        try:
            with open('bot/data/names.json', 'r+') as f:
                names = json.load(f)
                if name not in names:
                    names.append(name)
                    f.seek(0)
                    json.dump(names, f)
                    f.truncate()
                    return True
            return False
        except (FileNotFoundError, json.JSONDecodeError):
            return False
