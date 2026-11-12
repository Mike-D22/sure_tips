from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.core.mail import send_mail
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

@api_view(['POST'])
@permission_classes([AllowAny])
def contact_us(request):
    """
    General Contact Us API - Send email message from users
    """
    try:
        # Extract data from request
        name = request.data.get('name', '')
        email = request.data.get('email', '')
        message = request.data.get('message', '')
        subject = request.data.get('subject', 'Contact Us Form Submission')

        # Prepare email content
        email_subject = f"Contact Us: {subject} from {name}"

        # Create simple email message
        email_message = f"""
Name: {name}
Email: {email}
Subject: {subject}

Message:
{message}

---
Sent from your app contact form
        """.strip()

        # Get recipient email from settings or use default
        recipient_email = getattr(settings, 'CONTACT_EMAIL', settings.DEFAULT_FROM_EMAIL)

        # Send email
        send_mail(
            subject=email_subject,
            message=email_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient_email],
            fail_silently=False,
        )

        # Log the contact form submission
        logger.info(f"📧 Contact form submitted: {name} ({email}) - {subject}")

        return Response({
            'status': 'success',
            'message': 'Thank you for your message! We will get back to you soon.'
        }, status=200)

    except Exception as e:
        logger.error(f"Contact form error: {str(e)}")
        return Response({'error': 'Failed to send your message. Please try again later.'}, status=500)