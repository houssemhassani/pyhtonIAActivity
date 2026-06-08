# ia-server/app.py
"""
Serveur IA pour le calcul de productivité des employés en télétravail
Version: 3.0.0 - Version complète avec heures supplémentaires, projets et pauses
"""

from flask import Flask, request, jsonify, g
from flask_cors import CORS
import numpy as np
import logging
import hashlib
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from enum import Enum
import os
import time
from functools import wraps
import requests
from concurrent.futures import ThreadPoolExecutor

# ============================================
# CONFIGURATION
# ============================================

class Config:
    """Configuration centralisée du serveur IA"""
    
    # Serveur
    PORT = int(os.environ.get('IA_PORT', 5001))
    DEBUG = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    # Cache
    CACHE_ENABLED = os.environ.get('CACHE_ENABLED', 'True').lower() == 'true'
    CACHE_TTL = int(os.environ.get('CACHE_TTL', 300))  # 5 minutes
    
    # Strapi URL pour récupérer des données supplémentaires
    STRAPI_URL = os.environ.get('STRAPI_URL', 'http://localhost:1337')
    STRAPI_TOKEN = os.environ.get('STRAPI_TOKEN', '')
    
    # Seuils configurables
    IDENTICAL_PENALTY = float(os.environ.get('IDENTICAL_PENALTY', 30))
    LOW_ACTIVITY_PENALTY = float(os.environ.get('LOW_ACTIVITY_PENALTY', 15))
    HIGH_ACTIVITY_BONUS = float(os.environ.get('HIGH_ACTIVITY_BONUS', 10))
    LATE_PENALTY = float(os.environ.get('LATE_PENALTY', 5))
    KEYBOARD_BONUS_THRESHOLD = int(os.environ.get('KEYBOARD_BONUS_THRESHOLD', 5000))
    MOUSE_BONUS_THRESHOLD = int(os.environ.get('MOUSE_BONUS_THRESHOLD', 3000))
    KEYBOARD_BONUS = float(os.environ.get('KEYBOARD_BONUS', 5))
    MOUSE_BONUS = float(os.environ.get('MOUSE_BONUS', 5))
    EXPECTED_DAILY_HOURS = float(os.environ.get('EXPECTED_DAILY_HOURS', 8))
    TASKS_BONUS_FACTOR = float(os.environ.get('TASKS_BONUS_FACTOR', 0.15))
    
    # Nouveaux seuils pour heures supplémentaires et projets
    OVERTIME_BONUS_FACTOR = float(os.environ.get('OVERTIME_BONUS_FACTOR', 0.5))
    PROJECTS_BONUS_FACTOR = float(os.environ.get('PROJECTS_BONUS_FACTOR', 0.1))
    MAX_OVERTIME_BONUS = float(os.environ.get('MAX_OVERTIME_BONUS', 10))
    MAX_PROJECTS_BONUS = float(os.environ.get('MAX_PROJECTS_BONUS', 5))
    
    # Rate limiting
    RATE_LIMIT_PER_MINUTE = int(os.environ.get('RATE_LIMIT_PER_MINUTE', 30))
    RATE_LIMIT_PER_HOUR = int(os.environ.get('RATE_LIMIT_PER_HOUR', 200))
    
    # Seuil minimal de données pour calcul fiable
    MIN_SCREENSHOTS_FOR_RELIABLE_SCORE = 10
    MIN_ACTIVITY_LOGS_FOR_RELIABLE_SCORE = 20


class ScoreLevel(Enum):
    """Niveaux de score de productivité"""
    EXCELLENT = ("Excellent", 90, "🎉", "#10b981")
    GOOD = ("Bon", 75, "👍", "#3b82f6")
    AVERAGE = ("Moyen", 60, "📊", "#f59e0b")
    LOW = ("Faible", 40, "⚠️", "#ef4444")
    CRITICAL = ("Critique", 0, "🔴", "#dc2626")
    
    def __init__(self, label: str, min_score: int, icon: str, color: str):
        self.label = label
        self.min_score = min_score
        self.icon = icon
        self.color = color
    
    @classmethod
    def from_score(cls, score: int):
        for level in cls:
            if score >= level.min_score:
                return level
        return cls.CRITICAL


# ============================================
# SYSTÈME DE CACHE
# ============================================

class SimpleCache:
    """Cache simple en mémoire"""
    
    def __init__(self):
        self._cache = {}
        self._expiry = {}
    
    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            if self._expiry.get(key, 0) > time.time():
                return self._cache[key]
            else:
                del self._cache[key]
                del self._expiry[key]
        return None
    
    def set(self, key: str, value: Any, ttl: int = 300):
        self._cache[key] = value
        self._expiry[key] = time.time() + ttl
    
    def clear(self, pattern: str = None):
        if pattern:
            keys_to_delete = [k for k in self._cache.keys() if pattern in k]
            for k in keys_to_delete:
                del self._cache[k]
                del self._expiry[k]
        else:
            self._cache.clear()
            self._expiry.clear()
    
    def get_stats(self) -> Dict:
        return {'size': len(self._cache), 'enabled': True}


# ============================================
# LOGGING
# ============================================

def setup_logging():
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    os.makedirs('logs', exist_ok=True)
    
    logging.basicConfig(
        level=getattr(logging, os.environ.get('LOG_LEVEL', 'INFO')),
        format=log_format,
        handlers=[
            logging.FileHandler('logs/ia_server.log', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# ============================================
# CACHE INITIALIZATION
# ============================================

cache = SimpleCache() if Config.CACHE_ENABLED else None
if cache:
    logger.info("✅ Cache mémoire activé")

# ============================================
# DATA VALIDATION MODELS (COMPLET)
# ============================================

@dataclass
class ProductivityInput:
    """Modèle de validation des données d'entrée - Version complète"""
    employee_id: int
    screenshots: Dict[str, Any]
    activity: Dict[str, Any]
    attendance: Dict[str, Any]
    tasks: Dict[str, Any]
    
    # NOUVEAUX CHAMPS
    overtime_hours: float = 0.0
    projects_count: int = 0
    declared_breaks: Dict[str, Any] = field(default_factory=dict)
    expected_hours_per_day: float = 8.0
    working_days_in_period: int = 20
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'ProductivityInput':
        """Valide et convertit les données d'entrée"""
        if not data:
            raise ValueError("Aucune donnée reçue")
        
        required_fields = ['employee_id', 'screenshots', 'activity', 'attendance', 'tasks']
        missing_fields = [f for f in required_fields if f not in data]
        
        if missing_fields:
            raise ValueError(f"Champs manquants: {missing_fields}")
        
        if not isinstance(data['employee_id'], (int, float)):
            raise ValueError("employee_id doit être un nombre")
        
        employee_id = int(data['employee_id'])
        
        screenshots = {
            'total': data['screenshots'].get('total', 0),
            'identical': data['screenshots'].get('identical', 0),
            'avg_similarity': data['screenshots'].get('avg_similarity', 100)
        }
        
        activity = {
            'avg_activity_level': data['activity'].get('avg_activity_level', 50),
            'keyboard_clicks': data['activity'].get('keyboard_clicks', 0),
            'mouse_clicks': data['activity'].get('mouse_clicks', 0)
        }
        
        attendance = {
            'days_present': data['attendance'].get('days_present', 0),
            'days_late': data['attendance'].get('days_late', 0),
            'total_work_hours': data['attendance'].get('total_work_hours', 0)
        }
        
        tasks = {
            'total': data['tasks'].get('total', 0),
            'completed': data['tasks'].get('completed', 0),
            'completion_rate': data['tasks'].get('completion_rate', 0)
        }
        
        # Nouveaux champs avec valeurs par défaut
        overtime_hours = float(data.get('overtime_hours', 0))
        projects_count = int(data.get('projects_count', 0))
        declared_breaks = data.get('declared_breaks', {})
        expected_hours_per_day = float(data.get('expected_hours_per_day', Config.EXPECTED_DAILY_HOURS))
        working_days_in_period = int(data.get('working_days_in_period', 20))
        
        return cls(
            employee_id=employee_id,
            screenshots=screenshots,
            activity=activity,
            attendance=attendance,
            tasks=tasks,
            overtime_hours=overtime_hours,
            projects_count=projects_count,
            declared_breaks=declared_breaks,
            expected_hours_per_day=expected_hours_per_day,
            working_days_in_period=working_days_in_period
        )


# ============================================
# PRODUCTIVITY CALCULATOR (COMPLET)
# ============================================

class ProductivityCalculator:
    """Calculateur de score de productivité avec logique métier complète"""
    
    def __init__(self, config: Config = None):
        self.config = config or Config()
    
    def calculate(self, data: ProductivityInput) -> Dict[str, Any]:
        """Calcule le score de productivité complet"""
        score = 100.0
        penalties = []
        bonuses = []
        
        # 1. Pénalité pour captures identiques
        identical_penalty = self._calculate_identical_penalty(data.screenshots)
        score -= identical_penalty
        if identical_penalty > 0:
            penalties.append({
                'name': 'Captures identiques',
                'value': -round(identical_penalty, 1),
                'description': f"{identical_penalty:.1f} points - Taux de similarité élevé"
            })
        
        # 2. Bonus/Pénalité activité
        activity_adjustment = self._calculate_activity_adjustment(data.activity)
        score += activity_adjustment
        if activity_adjustment > 0:
            bonuses.append({
                'name': 'Activité élevée',
                'value': round(activity_adjustment, 1),
                'description': f"+{activity_adjustment:.1f} points - Bon niveau d'activité"
            })
        elif activity_adjustment < 0:
            penalties.append({
                'name': 'Activité faible',
                'value': round(activity_adjustment, 1),
                'description': f"{activity_adjustment:.1f} points - Niveau d'activité insuffisant"
            })
        
        # 3. Pénalité/Bonus heures
        hours_result = self._calculate_hours_adjustment(data)
        score -= hours_result['penalty']
        score += hours_result.get('bonus', 0)
        
        if hours_result['penalty'] > 0:
            penalties.append({
                'name': 'Heures manquantes',
                'value': -round(hours_result['penalty'], 1),
                'description': hours_result['description']
            })
        elif hours_result.get('bonus', 0) > 0:
            bonuses.append({
                'name': 'Heures supplémentaires',
                'value': round(hours_result['bonus'], 1),
                'description': hours_result['description']
            })
        
        # 4. Bonus heures supplémentaires déclarées
        overtime_bonus = self._calculate_overtime_bonus(data)
        score += overtime_bonus
        if overtime_bonus > 0:
            bonuses.append({
                'name': 'Heures supplémentaires déclarées',
                'value': round(overtime_bonus, 1),
                'description': f"+{overtime_bonus:.1f} points - {data.overtime_hours:.1f}h supplémentaires validées"
            })
        
        # 5. Bonus projets actifs
        projects_bonus = self._calculate_projects_bonus(data)
        score += projects_bonus
        if projects_bonus > 0:
            bonuses.append({
                'name': 'Projets actifs',
                'value': round(projects_bonus, 1),
                'description': f"+{projects_bonus:.1f} points - {data.projects_count} projet(s) actif(s)"
            })
        
        # 6. Bonus tâches complétées
        tasks_bonus = self._calculate_tasks_bonus(data.tasks)
        score += tasks_bonus
        if tasks_bonus > 0:
            bonuses.append({
                'name': 'Tâches complétées',
                'value': round(tasks_bonus, 1),
                'description': f"+{tasks_bonus:.1f} points - {data.tasks.get('completion_rate', 0):.0f}% des tâches terminées"
            })
        
        # 7. Pénalité retards
        late_penalty = self._calculate_late_penalty(data.attendance)
        score -= late_penalty
        if late_penalty > 0:
            penalties.append({
                'name': 'Retards',
                'value': -round(late_penalty, 1),
                'description': f"{late_penalty:.1f} points - {data.attendance.get('days_late', 0)} jour(s) de retard"
            })
        
        # 8. Bonus clics
        clicks_bonus = self._calculate_clicks_bonus(data.activity)
        score += clicks_bonus
        if clicks_bonus > 0:
            bonuses.append({
                'name': 'Activité clics',
                'value': round(clicks_bonus, 1),
                'description': f"+{clicks_bonus:.1f} points - Activité soutenue"
            })
        
        # 9. Fiabilité du score (si peu de données)
        reliability_adjustment = self._calculate_reliability_adjustment(data)
        if reliability_adjustment != 0:
            score += reliability_adjustment
            if reliability_adjustment < 0:
                penalties.append({
                    'name': 'Peu de données',
                    'value': reliability_adjustment,
                    'description': f"{abs(reliability_adjustment):.1f} points - Données insuffisantes pour un calcul fiable"
                })
        
        # Normalisation
        final_score = max(0, min(100, int(round(score))))
        
        return {
            'score': final_score,
            'raw_score': round(score, 2),
            'penalties': penalties,
            'bonuses': bonuses,
            'level': ScoreLevel.from_score(final_score),
            'activity_percentage': self._calculate_activity_percentage(data),
            'attendance_percentage': self._calculate_attendance_percentage(data.attendance),
            'tasks_percentage': data.tasks.get('completion_rate', 0),
            'overtime_hours_used': data.overtime_hours,
            'projects_count': data.projects_count,
            'reliability_score': self._calculate_reliability_score(data)
        }
    
    def _calculate_identical_penalty(self, screenshots: Dict) -> float:
        total = screenshots.get('total', 0)
        identical = screenshots.get('identical', 0)
        
        if total == 0:
            return 0
        
        identical_rate = identical / total
        penalty = identical_rate * self.config.IDENTICAL_PENALTY
        
        if identical_rate > 0.5:
            penalty += (identical_rate - 0.5) * 20
        
        return min(penalty, 50)
    
    def _calculate_activity_adjustment(self, activity: Dict) -> float:
        avg_level = activity.get('avg_activity_level', 50)
        
        if avg_level > 80:
            return self.config.HIGH_ACTIVITY_BONUS
        elif avg_level < 50:
            return -self.config.LOW_ACTIVITY_PENALTY
        return 0
    
    def _calculate_hours_adjustment(self, data: ProductivityInput) -> Dict:
        days_present = data.attendance.get('days_present', 0)
        total_work_hours = data.attendance.get('total_work_hours', 0)
        
        if days_present == 0:
            return {'penalty': 0, 'bonus': 0, 'description': ''}
        
        expected_hours = data.expected_hours_per_day * days_present
        
        if total_work_hours < expected_hours:
            missing_hours = expected_hours - total_work_hours
            missing_rate = missing_hours / expected_hours
            penalty = missing_rate * 25
            
            if missing_rate > 0.2:
                penalty += (missing_rate - 0.2) * 15
            
            penalty = min(penalty, 40)
            return {
                'penalty': penalty,
                'bonus': 0,
                'description': f"{penalty:.1f} points - {missing_hours:.1f}h manquantes sur {expected_hours:.1f}h attendues"
            }
        
        # Bonus heures supplémentaires (travail réel)
        if total_work_hours > expected_hours:
            overtime_rate = (total_work_hours - expected_hours) / expected_hours
            bonus = min(overtime_rate * 10, 5)
            return {
                'penalty': 0,
                'bonus': bonus,
                'description': f"+{bonus:.1f} points - {total_work_hours - expected_hours:.1f}h supplémentaires travaillées"
            }
        
        return {'penalty': 0, 'bonus': 0, 'description': ''}
    
    def _calculate_overtime_bonus(self, data: ProductivityInput) -> float:
        """Calcule le bonus pour heures supplémentaires déclarées"""
        if data.overtime_hours <= 0:
            return 0
        
        max_overtime_bonus = self.config.MAX_OVERTIME_BONUS
        bonus = min(data.overtime_hours * self.config.OVERTIME_BONUS_FACTOR, max_overtime_bonus)
        
        return bonus
    
    def _calculate_projects_bonus(self, data: ProductivityInput) -> float:
        """Calcule le bonus pour projets actifs"""
        if data.projects_count <= 0:
            return 0
        
        max_projects_bonus = self.config.MAX_PROJECTS_BONUS
        bonus = min(data.projects_count * self.config.PROJECTS_BONUS_FACTOR, max_projects_bonus)
        
        return bonus
    
    def _calculate_tasks_bonus(self, tasks: Dict) -> float:
        completion_rate = tasks.get('completion_rate', 0)
        return completion_rate * self.config.TASKS_BONUS_FACTOR
    
    def _calculate_late_penalty(self, attendance: Dict) -> float:
        days_late = attendance.get('days_late', 0)
        
        if days_late <= 2:
            return days_late * self.config.LATE_PENALTY
        elif days_late <= 5:
            return 10 + (days_late - 2) * 8
        else:
            return 30 + (days_late - 5) * 5
    
    def _calculate_clicks_bonus(self, activity: Dict) -> float:
        bonus = 0
        keyboard_clicks = activity.get('keyboard_clicks', 0)
        mouse_clicks = activity.get('mouse_clicks', 0)
        
        if keyboard_clicks > self.config.KEYBOARD_BONUS_THRESHOLD:
            bonus += self.config.KEYBOARD_BONUS
        if keyboard_clicks > self.config.KEYBOARD_BONUS_THRESHOLD * 1.5:
            bonus += self.config.KEYBOARD_BONUS
        
        if mouse_clicks > self.config.MOUSE_BONUS_THRESHOLD:
            bonus += self.config.MOUSE_BONUS
        if mouse_clicks > self.config.MOUSE_BONUS_THRESHOLD * 1.5:
            bonus += self.config.MOUSE_BONUS
        
        return min(bonus, 15)
    
    def _calculate_activity_percentage(self, data: ProductivityInput) -> float:
        avg_activity = data.activity.get('avg_activity_level', 50)
        
        keyboard_clicks = data.activity.get('keyboard_clicks', 0)
        mouse_clicks = data.activity.get('mouse_clicks', 0)
        
        click_factor = min(20, (keyboard_clicks / 10000) * 10 + (mouse_clicks / 5000) * 10)
        
        return min(100, avg_activity + click_factor)
    
    def _calculate_attendance_percentage(self, attendance: Dict) -> float:
        days_present = attendance.get('days_present', 0)
        total_work_hours = attendance.get('total_work_hours', 0)
        
        if days_present == 0:
            return 0
        
        expected_hours = self.config.EXPECTED_DAILY_HOURS * days_present
        if expected_hours == 0:
            return 0
        
        return min(100, (total_work_hours / expected_hours) * 100)
    
    def _calculate_reliability_score(self, data: ProductivityInput) -> int:
        """Calcule un score de fiabilité (0-100) basé sur la quantité de données"""
        score = 100
        
        total_screenshots = data.screenshots.get('total', 0)
        if total_screenshots < self.config.MIN_SCREENSHOTS_FOR_RELIABLE_SCORE:
            score -= 20
        
        total_activity_logs = data.activity.get('activity_logs_count', 0)
        if total_activity_logs < self.config.MIN_ACTIVITY_LOGS_FOR_RELIABLE_SCORE:
            score -= 20
        
        if data.tasks.get('total', 0) == 0:
            score -= 10
        
        return max(0, score)
    
    def _calculate_reliability_adjustment(self, data: ProductivityInput) -> float:
        """Retourne un ajustement (négatif) si peu de données"""
        reliability_score = self._calculate_reliability_score(data)
        
        if reliability_score < 50:
            return -10
        elif reliability_score < 70:
            return -5
        elif reliability_score < 85:
            return -2
        return 0


# ============================================
# RECOMMENDATION GENERATOR (COMPLET)
# ============================================

class RecommendationGenerator:
    """Générateur de recommandations personnalisées"""
    
    @staticmethod
    def generate(data: ProductivityInput, score_result: Dict) -> List[Dict]:
        recommendations = []
        
        # 1. Captures identiques
        total = data.screenshots.get('total', 0)
        identical = data.screenshots.get('identical', 0)
        
        if total > 0:
            identical_rate = identical / total if total > 0 else 0
            if identical > 5:
                recommendations.append({
                    'type': 'screenshots',
                    'priority': 'high',
                    'message': "🖼️ Trop de captures identiques détectées",
                    'action': "Variez vos tâches toutes les heures. Évitez de rester sur la même activité longtemps.",
                    'impact': f"Pénalité de {score_result['penalties'][0]['value'] if score_result['penalties'] else 0} points"
                })
            elif identical > 2:
                recommendations.append({
                    'type': 'screenshots',
                    'priority': 'medium',
                    'message': "📸 Plusieurs captures similaires",
                    'action': "Essayez d'alterner entre différentes applications et tâches.",
                    'impact': "Cela améliorera votre score de productivité"
                })
        
        # 2. Activité
        avg_activity = data.activity.get('avg_activity_level', 50)
        if avg_activity < 40:
            recommendations.append({
                'type': 'activity',
                'priority': 'high',
                'message': "📈 Niveau d'activité très faible",
                'action': "Augmentez votre activité régulière : répondez aux messages, naviguez, tapez au clavier.",
                'impact': "L'activité représente 10-20% de votre score"
            })
        elif avg_activity < 60:
            recommendations.append({
                'type': 'activity',
                'priority': 'medium',
                'message': "📊 Activité modérée",
                'action': "Maintenez une activité constante tout au long de la journée.",
                'impact': "Essayez d'atteindre 70% d'activité pour un bonus"
            })
        
        # 3. Retards
        days_late = data.attendance.get('days_late', 0)
        if days_late > 0:
            recommendations.append({
                'type': 'punctuality',
                'priority': 'high' if days_late > 3 else 'medium',
                'message': f"⏰ {days_late} jour(s) de retard ce mois",
                'action': "Préparez-vous la veille : checklist matinale, réveil 30 min plus tôt.",
                'impact': f"Pénalité de {days_late * 5} à {days_late * 8} points"
            })
        
        # 4. Tâches
        completion_rate = data.tasks.get('completion_rate', 0)
        total_tasks = data.tasks.get('total', 0)
        
        if total_tasks > 0:
            if completion_rate < 40:
                recommendations.append({
                    'type': 'tasks',
                    'priority': 'high',
                    'message': "✅ Taux de complétion des tâches très faible",
                    'action': "Priorisez vos tâches : commencez par les plus importantes. Fixez-vous 2-3 objectifs quotidiens.",
                    'impact': "Les tâches complétées peuvent augmenter votre score jusqu'à 15 points"
                })
            elif completion_rate < 70:
                recommendations.append({
                    'type': 'tasks',
                    'priority': 'low',
                    'message': "📋 Des tâches sont encore en cours",
                    'action': "Concentrez-vous sur la finalisation des tâches commencées avant d'en démarrer de nouvelles.",
                    'impact': "Fini ce que vous commencez"
                })
        
        # 5. Heures travaillées
        days_present = data.attendance.get('days_present', 0)
        total_hours = data.attendance.get('total_work_hours', 0)
        
        if days_present > 0:
            avg_hours = total_hours / days_present
            if avg_hours < 6:
                recommendations.append({
                    'type': 'work_hours',
                    'priority': 'high',
                    'message': f"💪 {avg_hours:.1f}h en moyenne par jour (objectif: 8h)",
                    'action': "Structurez votre journée : blocs de 2h de travail, pauses courtes, suivi du temps.",
                    'impact': "Chaque heure manquante réduit votre score"
                })
            elif avg_hours < 7:
                recommendations.append({
                    'type': 'work_hours',
                    'priority': 'low',
                    'message': f"⏱️ {avg_hours:.1f}h par jour",
                    'action': "Ajoutez une heure de travail ciblée chaque jour pour atteindre l'objectif.",
                    'impact': "+5 à 10 points potentiels"
                })
        
        # 6. Heures supplémentaires
        if data.overtime_hours > 0:
            recommendations.append({
                'type': 'overtime',
                'priority': 'low',
                'message': f"⏰ {data.overtime_hours:.1f}h supplémentaires déclarées ce mois",
                'action': "Continuez à déclarer vos heures supplémentaires pour valoriser votre investissement.",
                'impact': "Bonus de +" + str(min(data.overtime_hours * 0.5, 10)) + " points"
            })
        
        # 7. Projets
        if data.projects_count > 0:
            recommendations.append({
                'type': 'projects',
                'priority': 'low',
                'message': f"📁 {data.projects_count} projet(s) actif(s)",
                'action': "Maintenez une bonne communication sur chaque projet et mettez à jour régulièrement l'avancement.",
                'impact': "Bonus de +" + str(min(data.projects_count * 0.1, 5)) + " points"
            })
        
        # 8. Félicitations
        if score_result['score'] >= 85:
            recommendations.insert(0, {
                'type': 'congrats',
                'priority': 'success',
                'message': "🎉 Excellent travail ! Productivité remarquable",
                'action': "Continuez ainsi ! Partagez vos méthodes avec l'équipe.",
                'impact': "Vous êtes parmi les meilleurs"
            })
        elif score_result['score'] >= 70:
            recommendations.append({
                'type': 'encouragement',
                'priority': 'low',
                'message': "👍 Bonne productivité, vous êtes sur la bonne voie",
                'action': "Quelques ajustements ciblés peuvent vous rendre encore plus performant.",
                'impact': "Visez les 85+ pour le niveau Excellent"
            })
        
        # 9. Recommandation fiabilité
        reliability_score = score_result.get('reliability_score', 100)
        if reliability_score < 70:
            recommendations.append({
                'type': 'reliability',
                'priority': 'medium',
                'message': "📊 Données limitées - Score moins fiable",
                'action': "Utilisez l'application plus régulièrement pour un suivi plus précis.",
                'impact': "Plus de données = meilleure analyse"
            })
        
        priority_order = {'high': 0, 'medium': 1, 'low': 2, 'success': 3}
        recommendations.sort(key=lambda x: priority_order.get(x['priority'], 2))
        
        return recommendations[:6]


# ============================================
# RATE LIMITING
# ============================================

class SimpleRateLimiter:
    def __init__(self):
        self._requests = {}
    
    def _cleanup(self):
        now = time.time()
        expired = []
        for key, data in self._requests.items():
            if now - data['timestamp'] > 3600:
                expired.append(key)
        for key in expired:
            del self._requests[key]
    
    def check_limit(self, key: str, limit_per_minute: int = 30, limit_per_hour: int = 200) -> tuple[bool, str]:
        self._cleanup()
        now = time.time()
        
        if key not in self._requests:
            self._requests[key] = {'minute_count': 1, 'hour_count': 1, 'timestamp': now}
            return True, ""
        
        data = self._requests[key]
        
        if now - data['timestamp'] < 60:
            if data['minute_count'] >= limit_per_minute:
                return False, f"Trop de requêtes par minute. Maximum: {limit_per_minute}"
            data['minute_count'] += 1
        else:
            data['minute_count'] = 1
            data['timestamp'] = now
        
        if now - data['timestamp'] < 3600:
            if data['hour_count'] >= limit_per_hour:
                return False, f"Trop de requêtes par heure. Maximum: {limit_per_hour}"
            data['hour_count'] += 1
        else:
            data['hour_count'] = 1
        
        return True, ""


rate_limiter = SimpleRateLimiter()


# ============================================
# DÉCORATEURS
# ============================================

def rate_limit(limit_per_minute: int = None, limit_per_hour: int = None):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            client_ip = request.remote_addr or 'unknown'
            allowed, message = rate_limiter.check_limit(
                client_ip,
                limit_per_minute or Config.RATE_LIMIT_PER_MINUTE,
                limit_per_hour or Config.RATE_LIMIT_PER_HOUR
            )
            if not allowed:
                logger.warning(f"Rate limit exceeded for {client_ip}: {message}")
                return jsonify({
                    'success': False,
                    'error': message,
                    'code': 'RATE_LIMIT_EXCEEDED'
                }), 429
            return func(*args, **kwargs)
        return wrapper
    return decorator


def cache_response(ttl: int = None):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not cache:
                return func(*args, **kwargs)
            try:
                request_data = request.get_json(silent=True)
                if request_data:
                    cache_data = {k: v for k, v in request_data.items() if k != 'screenshots'}
                    cache_key = f"ia_score:{hashlib.md5(json.dumps(cache_data, sort_keys=True).encode()).hexdigest()}"
                    cached = cache.get(cache_key)
                    if cached:
                        logger.info(f"✅ Cache hit pour {cache_key}")
                        response = jsonify(cached)
                        response.status_code = 200
                        return response
                    response = func(*args, **kwargs)
                    if response.status_code == 200:
                        response_data = response.get_json()
                        if response_data and response_data.get('success'):
                            cache.set(cache_key, response_data, ttl or Config.CACHE_TTL)
                            logger.info(f"💾 Cache mis à jour pour {cache_key}")
                    return response
            except Exception as e:
                logger.warning(f"Cache error: {e}")
            return func(*args, **kwargs)
        return wrapper
    return decorator


def validate_input():
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                data = request.get_json()
                if not data:
                    return jsonify({
                        'success': False,
                        'error': "Aucune donnée reçue",
                        'code': 'NO_DATA'
                    }), 400
                validated_data = ProductivityInput.from_dict(data)
                g.validated_data = validated_data
                return func(*args, **kwargs)
            except ValueError as e:
                logger.warning(f"Validation échouée: {e}")
                return jsonify({
                    'success': False,
                    'error': str(e),
                    'code': 'VALIDATION_ERROR'
                }), 400
            except Exception as e:
                logger.error(f"Erreur validation inattendue: {e}")
                return jsonify({
                    'success': False,
                    'error': "Format de données invalide",
                    'code': 'PARSE_ERROR'
                }), 400
        return wrapper
    return decorator


# ============================================
# FLASK APP
# ============================================

app = Flask(__name__)

CORS(app, resources={
    r"/api/*": {
        "origins": ["http://localhost:4200", "http://localhost:1337", "http://localhost:5000", "http://localhost:5001"],
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})


# ============================================
# API ENDPOINTS
# ============================================

@app.route('/api/calculate-score', methods=['POST'])
@rate_limit(limit_per_minute=30, limit_per_hour=200)
@validate_input()
@cache_response(ttl=Config.CACHE_TTL)
def calculate_score():
    """Calcule le score de productivité d'un employé"""
    try:
        validated_data = g.validated_data
        logger.info(f"🤖 Calcul du score pour employé: {validated_data.employee_id}")
        
        calculator = ProductivityCalculator()
        score_result = calculator.calculate(validated_data)
        recommendations = RecommendationGenerator.generate(validated_data, score_result)
        
        response = {
            'success': True,
            'employee_id': validated_data.employee_id,
            'score': score_result['score'],
            'raw_score': score_result['raw_score'],
            'level': score_result['level'].label,
            'level_icon': score_result['level'].icon,
            'level_color': score_result['level'].color,
            'recommendations': [r['message'] for r in recommendations],
            'recommendations_detailed': recommendations,
            'details': {
                'productivity': score_result['score'],
                'activity_score': score_result['activity_percentage'],
                'tasks_score': score_result['tasks_percentage'],
                'attendance_score': score_result['attendance_percentage'],
                'penalties': score_result['penalties'],
                'bonuses': score_result['bonuses'],
                'overtime_hours': score_result.get('overtime_hours_used', 0),
                'projects_count': score_result.get('projects_count', 0),
                'reliability_score': score_result.get('reliability_score', 100)
            },
            'metadata': {
                'calculated_at': datetime.now().isoformat(),
                'version': '3.0.0',
                'cache_hit': False
            }
        }
        
        logger.info(f"✅ Score calculé: {score_result['score']} pour employé {validated_data.employee_id}")
        return jsonify(response)
        
    except Exception as e:
        logger.error(f"❌ Erreur lors du calcul: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': "Erreur interne du serveur IA",
            'code': 'INTERNAL_ERROR',
            'message': str(e) if Config.DEBUG else None
        }), 500


@app.route('/api/batch-score', methods=['POST'])
@rate_limit(limit_per_minute=10, limit_per_hour=100)
def batch_score():
    """Calcule les scores pour plusieurs employés"""
    try:
        data = request.get_json()
        employees_data = data.get('employees', [])
        
        if not employees_data:
            return jsonify({
                'success': False,
                'error': "Liste d'employés vide",
                'code': 'EMPTY_LIST'
            }), 400
        
        if len(employees_data) > 50:
            return jsonify({
                'success': False,
                'error': "Maximum 50 employés par requête",
                'code': 'TOO_MANY'
            }), 400
        
        logger.info(f"🤖 Batch calculation pour {len(employees_data)} employés")
        
        results = []
        calculator = ProductivityCalculator()
        
        for emp_data in employees_data:
            try:
                validated = ProductivityInput.from_dict(emp_data)
                score_result = calculator.calculate(validated)
                results.append({
                    'employee_id': validated.employee_id,
                    'score': score_result['score'],
                    'level': score_result['level'].label,
                    'level_icon': score_result['level'].icon,
                    'success': True
                })
            except Exception as e:
                logger.warning(f"Erreur pour employé {emp_data.get('employee_id', 'unknown')}: {e}")
                results.append({
                    'employee_id': emp_data.get('employee_id', 'unknown'),
                    'error': str(e),
                    'success': False
                })
        
        successful_scores = [r['score'] for r in results if r.get('success')]
        avg_score = sum(successful_scores) / len(successful_scores) if successful_scores else 0
        
        return jsonify({
            'success': True,
            'results': results,
            'summary': {
                'total': len(employees_data),
                'successful': len(successful_scores),
                'failed': len(employees_data) - len(successful_scores),
                'average_score': round(avg_score, 1),
                'best_score': max(successful_scores) if successful_scores else 0,
                'worst_score': min(successful_scores) if successful_scores else 0
            },
            'metadata': {
                'calculated_at': datetime.now().isoformat(),
                'version': '3.0.0'
            }
        })
        
    except Exception as e:
        logger.error(f"❌ Erreur batch: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'code': 'BATCH_ERROR'
        }), 500


@app.route('/api/health', methods=['GET'])
def health():
    """Endpoint de santé pour le monitoring"""
    status = {
        'status': 'healthy',
        'service': 'ia-server',
        'version': '3.0.0',
        'timestamp': datetime.now().isoformat(),
        'checks': {
            'cache': cache is not None if cache else False,
            'cache_stats': cache.get_stats() if cache else {'enabled': False, 'size': 0}
        }
    }
    return jsonify(status), 200


@app.route('/api/config', methods=['GET'])
@rate_limit(limit_per_minute=100, limit_per_hour=500)
def get_config():
    """Récupère la configuration du serveur IA"""
    return jsonify({
        'service': 'ia-server',
        'version': '3.0.0',
        'thresholds': {
            'identical_penalty': Config.IDENTICAL_PENALTY,
            'low_activity_penalty': Config.LOW_ACTIVITY_PENALTY,
            'high_activity_bonus': Config.HIGH_ACTIVITY_BONUS,
            'late_penalty': Config.LATE_PENALTY,
            'keyboard_bonus_threshold': Config.KEYBOARD_BONUS_THRESHOLD,
            'mouse_bonus_threshold': Config.MOUSE_BONUS_THRESHOLD,
            'expected_daily_hours': Config.EXPECTED_DAILY_HOURS,
            'tasks_bonus_factor': Config.TASKS_BONUS_FACTOR,
            'overtime_bonus_factor': Config.OVERTIME_BONUS_FACTOR,
            'projects_bonus_factor': Config.PROJECTS_BONUS_FACTOR,
            'max_overtime_bonus': Config.MAX_OVERTIME_BONUS,
            'max_projects_bonus': Config.MAX_PROJECTS_BONUS
        },
        'cache': {
            'enabled': Config.CACHE_ENABLED,
            'ttl_seconds': Config.CACHE_TTL
        },
        'rate_limits': {
            'per_minute': Config.RATE_LIMIT_PER_MINUTE,
            'per_hour': Config.RATE_LIMIT_PER_HOUR
        }
    })


@app.route('/api/clear-cache', methods=['POST'])
@rate_limit(limit_per_minute=5, limit_per_hour=20)
def clear_cache():
    """Vide le cache"""
    if not cache:
        return jsonify({
            'success': False,
            'message': 'Cache désactivé'
        }), 400
    
    try:
        cache.clear()
        return jsonify({
            'success': True,
            'message': 'Cache vidé avec succès'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/version', methods=['GET'])
def get_version():
    """Retourne la version du service"""
    return jsonify({
        'service': 'ia-server',
        'version': '3.0.0',
        'api_version': 'v1',
        'features': [
            'productivity_scoring',
            'recommendations',
            'batch_processing',
            'caching',
            'rate_limiting',
            'overtime_support',
            'projects_support'
        ]
    })


@app.route('/api/predict', methods=['POST'])
@rate_limit(limit_per_minute=20, limit_per_hour=100)
def predict_trend():
    """
    Prédit la tendance de productivité pour les prochains jours
    (Version simplifiée - à améliorer avec ML)
    """
    try:
        data = request.get_json()
        historical_scores = data.get('historical_scores', [])
        
        if not historical_scores or len(historical_scores) < 3:
            return jsonify({
                'success': False,
                'error': 'Données historiques insuffisantes',
                'code': 'INSUFFICIENT_DATA'
            }), 400
        
        # Calcul simple de tendance
        recent_scores = historical_scores[-7:] if len(historical_scores) > 7 else historical_scores
        avg_recent = sum(recent_scores) / len(recent_scores)
        avg_old = sum(historical_scores[:len(historical_scores)//2]) / (len(historical_scores)//2) if len(historical_scores) >= 6 else avg_recent
        
        trend = "up" if avg_recent > avg_old else "down" if avg_recent < avg_old else "stable"
        
        # Prédiction simple
        if trend == "up":
            predicted_score = min(100, avg_recent + 5)
        elif trend == "down":
            predicted_score = max(0, avg_recent - 5)
        else:
            predicted_score = avg_recent
        
        return jsonify({
            'success': True,
            'current_average': round(avg_recent, 1),
            'trend': trend,
            'predicted_score': round(predicted_score, 1),
            'confidence': min(90, len(historical_scores) * 5),
            'metadata': {
                'calculated_at': datetime.now().isoformat(),
                'data_points': len(historical_scores)
            }
        })
        
    except Exception as e:
        logger.error(f"❌ Erreur prédiction: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'code': 'PREDICTION_ERROR'
        }), 500


@app.errorhandler(404)
def not_found_handler(e):
    return jsonify({
        'success': False,
        'error': "Endpoint non trouvé",
        'code': 'NOT_FOUND'
    }), 404


@app.errorhandler(500)
def internal_error_handler(e):
    logger.error(f"Internal server error: {e}")
    return jsonify({
        'success': False,
        'error': "Erreur interne du serveur",
        'code': 'INTERNAL_SERVER_ERROR'
    }), 500


# ============================================
# MAIN
# ============================================

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 SERVEUR IA - REMOTE WORK SUPERVISOR v3.0.0")
    print("=" * 60)
    print(f"📦 Version: 3.0.0 (Complète avec heures sup et projets)")
    print(f"🌐 Port: {Config.PORT}")
    print(f"🐛 Debug mode: {Config.DEBUG}")
    print(f"💾 Cache: {'Activé' if cache else 'Désactivé'}")
    print(f"⏱️  Cache TTL: {Config.CACHE_TTL}s")
    print(f"🛡️ Rate limiting: {Config.RATE_LIMIT_PER_MINUTE}/min, {Config.RATE_LIMIT_PER_HOUR}/hour")
    print("=" * 60)
    print("📋 Endpoints disponibles:")
    print("   POST   /api/calculate-score  - Calculer un score")
    print("   POST   /api/batch-score      - Calcul multiple")
    print("   POST   /api/predict          - Prédire la tendance")
    print("   GET    /api/health           - État du service")
    print("   GET    /api/config           - Configuration")
    print("   POST   /api/clear-cache      - Vider le cache")
    print("   GET    /api/version          - Version")
    print("=" * 60)
    
    app.run(
        host='0.0.0.0',
        port=Config.PORT,
        debug=Config.DEBUG,
        threaded=True
    )